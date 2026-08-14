"""The write surface, and the one rule it is built around.

Nothing here writes application state. Every route publishes an event and the
pipeline folds it, so the response never reflects the result — which is the
awkward half of event sourcing and also the reason a correction survives the
next reprocess when a column write would not.

So most of these tests do the same thing twice: assert what the route put on the
queue, then drain the pipeline and assert what the row became.
"""

from typing import Any

import pytest
from httpx import AsyncClient

from loop.db import Database, Queue, claim
from loop.services import PipelineService

pytestmark = pytest.mark.integration


async def drain(db: Database) -> None:
    """Let the pipeline catch up, the way it would in a running system."""
    pipeline = PipelineService(db)
    for message in await claim(db, Queue.EVENT, batch=200, visibility=30):
        await pipeline.handle(message)


async def post(client: AsyncClient, path: str, body: Any = None) -> Any:
    token = (await client.get("/api/me")).json()["csrf"]
    return await client.post(path, json=body, headers={"x-csrf-token": token})


class TestQuickAdd:
    async def test_it_returns_the_four_keys_the_client_reads_back(
        self, client: AsyncClient
    ) -> None:
        response = await post(
            client,
            "/api/applications",
            {"company": "Prima", "role": "ML Engineer", "channel": "referral"},
        )
        assert response.status_code == 201
        assert list(response.json()) == ["id", "company", "role", "channel"]
        assert response.json()["company"] == "Prima"

    async def test_a_second_application_to_the_same_company_reuses_it(
        self, client: AsyncClient, db: Database
    ) -> None:
        # The upsert used to be `do update`, which needs UPDATE on `companies`;
        # migration 014 grants the gateway INSERT only, so in a real deployment
        # the *second* quick-add for a company failed on permissions before the
        # application was ever created. Tests connect as the owner, so the two
        # assertions that matter are below: this one covers the logic, and
        # `test_the_gateway_role_can_do_it` covers the grant.
        body = {"company": "Ripetuta", "role": "Backend Engineer"}
        first = (await post(client, "/api/applications", body)).json()
        second = await post(
            client, "/api/applications", {**body, "company": "ripetuta"}
        )

        assert second.status_code == 201
        async with db.untenanted() as connection:
            companies = await connection.fetch(
                "select company_id from applications where id = any($1::uuid[])",
                [first["id"], second.json()["id"]],
            )
        assert len({row["company_id"] for row in companies}) == 1

    async def test_the_gateway_role_can_do_it(self, db: Database) -> None:
        # The grant, asserted against the role the API actually runs as rather
        # than the owner the suite connects as. `do update` raises
        # InsufficientPrivilege here; `do nothing` plus a lookup does not.
        async with db.untenanted() as connection, connection.transaction():
            await connection.execute("set local role loop_gateway")
            for _ in range(2):
                company_id = await connection.fetchval(
                    """
                    insert into companies (canonical_name) values ('Grantata')
                    on conflict (lower(canonical_name), coalesce(domain, '')) do nothing
                    returning id
                    """,
                ) or await connection.fetchval(
                    """
                    select id from companies
                     where lower(canonical_name) = lower('Grantata')
                       and coalesce(domain, '') = ''
                    """
                )
                assert company_id is not None

    async def test_the_row_is_created_here_and_moved_by_the_pipeline(
        self, client: AsyncClient, db: Database
    ) -> None:
        created = (
            await post(
                client, "/api/applications", {"company": "Ayes", "role": "Data Engineer"}
            )
        ).json()

        # Before the fold: the seed values the insert wrote, and no channel,
        # because `sources` is the pipeline's to write.
        before = (await client.get(f"/api/applications/{created['id']}")).json()
        assert (before["stage"], before["applied_at"], before["channel"]) == (
            "applied",
            None,
            None,
        )

        await drain(db)

        after = (await client.get(f"/api/applications/{created['id']}")).json()
        assert after["applied_at"] is not None
        assert after["channel"] == "career_page"
        assert [event["what"] for event in after["events"]] == ["Applied"]
        # Rung 4 and no evidence: a human said so.
        assert after["events"][0]["source"] == "quick add"

    async def test_a_body_with_neither_shape_is_refused(
        self, client: AsyncClient
    ) -> None:
        response = await post(client, "/api/applications", {"role": "Engineer"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_body"

    async def test_a_channel_that_is_not_a_channel_is_refused(
        self, client: AsyncClient
    ) -> None:
        response = await post(
            client,
            "/api/applications",
            {"company": "Prima", "role": "Engineer", "channel": "carrier_pigeon"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["field"] == "channel"


class TestArchiving:
    async def test_archiving_as_dormant_goes_quiet_rather_than_withdrawn(
        self, client: AsyncClient, db: Database
    ) -> None:
        application_id = await _an_application(client, "Dinova")

        assert (
            await post(client, f"/api/applications/{application_id}/archive", {"as": "dormant"})
        ).json() == {"ok": True}
        await drain(db)

        row = (await client.get(f"/api/applications/{application_id}")).json()
        assert row["status"] == "dormant"
        assert row["closed"] is True
        # Archived by hand is not "closed by silence": the sweep's presumption
        # is a reading of the mailbox and this is a statement by the user.
        assert row["display_stage"] == "Dormant"

    async def test_the_bulk_route_counts_what_was_asked_for(
        self, client: AsyncClient, db: Database
    ) -> None:
        first = await _an_application(client, "Nexi")
        second = await _an_application(client, "Satispay")

        response = await post(
            client, "/api/applications/archive", {"ids": [first, second], "as": "withdrawn"}
        )

        assert response.json() == {"ok": True, "count": 2}
        await drain(db)
        for application_id in (first, second):
            row = (await client.get(f"/api/applications/{application_id}")).json()
            assert row["status"] == "withdrawn"

    async def test_an_empty_batch_is_refused(self, client: AsyncClient) -> None:
        response = await post(client, "/api/applications/archive", {"ids": []})
        assert response.status_code == 400
        assert response.json()["error"]["field"] == "ids"


class TestCorrections:
    async def test_a_correction_pins_the_field_it_names(
        self, client: AsyncClient, db: Database
    ) -> None:
        application_id = await _an_application(client, "Cradle")

        await post(
            client,
            f"/api/applications/{application_id}/correct",
            {"field": "role_title", "to": "Staff Engineer"},
        )
        await drain(db)

        row = (await client.get(f"/api/applications/{application_id}")).json()
        assert row["role"] == "Staff Engineer"
        assert row["events"][0]["what"] == "You corrected this"
        # The reference recorded the *status* as the before-value for every
        # field but `stage`, so this line read `role_title: live → Staff
        # Engineer`.
        assert row["events"][0]["detail"] == "role_title: None → Staff Engineer"

    async def test_a_stage_correction_records_what_it_replaced(
        self, client: AsyncClient, db: Database
    ) -> None:
        application_id = await _an_application(client, "Bending Spoons")

        await post(
            client,
            f"/api/applications/{application_id}/correct",
            {"field": "stage", "to": "hr_call"},
        )
        await drain(db)

        row = (await client.get(f"/api/applications/{application_id}")).json()
        assert row["stage"] == "hr_call"
        assert row["events"][0]["detail"] == "stage: applied → hr_call"

    async def test_a_field_with_no_editor_is_refused(self, client: AsyncClient) -> None:
        response = await post(
            client,
            "/api/applications/00000000-0000-0000-0000-000000000000/correct",
            {"field": "merge", "to": "split"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["field"] == "field"

    async def test_correcting_something_that_is_not_there_is_a_404(
        self, client: AsyncClient
    ) -> None:
        response = await post(
            client,
            "/api/applications/00000000-0000-0000-0000-000000000000/correct",
            {"field": "stage", "to": "hr_call"},
        )
        assert response.status_code == 404


class TestTheReviewQueue:
    async def test_answering_removes_the_item_and_keeps_only_the_shape(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        item_id = await _a_review_item(db, user_id)

        response = await post(
            client, f"/api/review/{item_id}", {"choice": {"kind": "new_application"}}
        )

        assert response.json() == {"ok": True}
        async with db.session(user_id) as connection:
            row = await connection.fetchrow(
                "select excerpt, resolution, learned_pattern from review_items where id = $1",
                item_id,
            )
        # The excerpt is gone the moment it has served its purpose, and what
        # survives carries no text, no company and no id.
        assert row["excerpt"] is None
        assert row["resolution"] == {"kind": "new_application"}
        assert row["learned_pattern"] == {"kind": "unknown_intent", "answer": "new_application"}

    async def test_declining_to_learn_keeps_nothing_at_all(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        item_id = await _a_review_item(db, user_id)

        await post(
            client,
            f"/api/review/{item_id}",
            {"choice": {"kind": "new_application"}, "learn": False},
        )

        async with db.session(user_id) as connection:
            assert (
                await connection.fetchval(
                    "select learned_pattern from review_items where id = $1", item_id
                )
                is None
            )

    async def test_answering_twice_is_a_404(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        item_id = await _a_review_item(db, user_id)
        answer = {"choice": {"kind": "intent", "intent": "rejected", "agree": False}}
        await post(client, f"/api/review/{item_id}", answer)

        again = await post(
            client, f"/api/review/{item_id}", {"choice": {"kind": "new_application"}}
        )
        assert again.status_code == 404


class TestSuggestions:
    async def test_dismissing_removes_the_card(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        await _a_suggestion(db, user_id, "follow_up_due:x")
        assert _keys(await client.get("/api/suggestions")) == ["follow_up_due:x"]

        await post(client, "/api/suggestions/follow_up_due:x/dismiss")

        assert _keys(await client.get("/api/suggestions")) == []

    async def test_later_hides_it_without_ending_it(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        await _a_suggestion(db, user_id, "prepare:y")

        await post(client, "/api/suggestions/prepare:y/snooze")

        assert _keys(await client.get("/api/suggestions")) == []
        async with db.session(user_id) as connection:
            row = await connection.fetchrow(
                "select snoozed_until, dismissed_at from suggestions where key = 'prepare:y'"
            )
        # Hidden, not answered: the nudge rule still counts it open, which is
        # why it comes back tomorrow without a second notification.
        assert row["snoozed_until"] is not None
        assert row["dismissed_at"] is None

    async def test_an_unknown_action_is_a_404(self, client: AsyncClient) -> None:
        assert (await post(client, "/api/suggestions/anything/detonate")).status_code == 404

    async def test_the_draft_is_composed_but_never_sendable(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        application_id = await _an_application(client, "Ayes")
        await drain(db)
        await _a_suggestion(db, user_id, "follow_up_due:z", application_id)

        body = (await client.get("/api/suggestions/follow_up_due:z/draft")).json()

        assert list(body) == ["subject", "body", "mailto_url", "can_send", "note"]
        assert body["subject"] == "Re: Ayes application"
        assert body["can_send"] is False
        # Percent-encoded, not form-encoded: a mailto query is not a form, and
        # the reference's `+` arrived in Apple Mail as a literal plus.
        assert "+" not in body["mailto_url"]
        assert "%20" in body["mailto_url"]

    async def test_a_suggestion_pointing_nowhere_has_no_draft(
        self, client: AsyncClient, db: Database, user_id: str
    ) -> None:
        await _a_suggestion(db, user_id, "let_it_go:none")
        response = await client.get("/api/suggestions/let_it_go:none/draft")
        assert response.status_code == 404
        assert response.json()["error"] == {
            "code": "not_found",
            "message": "no draft for that suggestion",
        }


class TestLeaving:
    async def test_the_confirmation_is_the_literal_word(
        self, client: AsyncClient
    ) -> None:
        token = (await client.get("/api/me")).json()["csrf"]
        response = await client.request(
            "DELETE",
            "/api/account",
            json={"confirm": "please"},
            headers={"x-csrf-token": token},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "confirm_required"


def _keys(response: Any) -> list[str]:
    return [s["key"] for s in response.json()["suggestions"]]


async def _an_application(client: AsyncClient, company: str) -> str:
    created = (
        await post(client, "/api/applications", {"company": company, "role": "Engineer"})
    ).json()
    return str(created["id"])


async def _a_review_item(db: Database, user_id: str) -> str:
    async with db.session(user_id) as connection:
        return str(
            await connection.fetchval(
                """
                insert into review_items (user_id, kind, evidence_ref, excerpt)
                values ($1,'unknown_intent','msg-1','"…" — someone@example.org')
                returning id
                """,
                user_id,
            )
        )


async def _a_suggestion(
    db: Database, user_id: str, key: str, application_id: str | None = None
) -> None:
    async with db.session(user_id) as connection:
        await connection.execute(
            """
            insert into suggestions (user_id, key, rule, application_ids, payload)
            values ($1,$2,$3,$4,'{}')
            """,
            user_id,
            key,
            key.split(":")[0],
            [application_id] if application_id else [],
        )
