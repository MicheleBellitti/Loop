from datetime import UTC, datetime

from loop.domain.messages import CalendarInvite, MessageHeaders, RawMessage
from loop.ladder import ClassifierContext, RuleRegistry, classify

# The real registry, so that "is this sender an ATS" is answered by the rule
# files rather than by a list written to make the test pass.
ATS = RuleRegistry.load().ats_domains


def msg(
    *,
    sender: str = "Talent <talent@nexi.it>",
    subject: str = "",
    text: str = "",
    thread_id: str | None = None,
    list_id: str | None = None,
    list_unsubscribe: str | None = None,
    precedence: str | None = None,
    invite: CalendarInvite | None = None,
) -> RawMessage:
    return RawMessage(
        user_id="u",
        mailbox_id="m",
        provider_message_id="id",
        thread_id=thread_id,
        received_at=datetime(2026, 7, 30, tzinfo=UTC),
        headers=MessageHeaders(
            message_id="<1@x>",
            sender=sender,
            subject=subject,
            date="",
            list_id=list_id,
            list_unsubscribe=list_unsubscribe,
            precedence=precedence,
        ),
        text=text,
        body_sha256="",
        invite=invite,
    )


def ctx(**over: object) -> ClassifierContext:
    base: dict[str, object] = {"ats_domains": ATS}
    base.update(over)
    return ClassifierContext(**base)  # type: ignore[arg-type]


class TestTheRecallBias:
    def test_an_ats_confirmation_passes(self) -> None:
        result = classify(
            msg(
                sender="Prima <no-reply@hire.eu.lever.co>",
                subject="Thank you for your application to Prima",
            ),
            ctx(),
        )
        assert result.outcome == "pass"

    def test_a_bulk_flagged_linkedin_confirmation_still_passes(self) -> None:
        # The single most common false negative in the whole system: their
        # confirmations carry the same bulk headers as their job alerts.
        result = classify(
            msg(
                sender="LinkedIn <jobs-noreply@linkedin.com>",
                subject="La tua candidatura è stata inviata a Casavo",
                list_id="<jobs.linkedin.com>",
                precedence="bulk",
            ),
            ctx(),
        )
        assert result.outcome == "pass"
        assert any("waived" in r for r in result.reasons)

    def test_the_waiver_covers_every_ats_not_only_the_two_named_ones(self) -> None:
        # An Italian ATS acknowledgement scored +2 for naming a candidatura and
        # -4 for arriving in bulk, landed at -2, and was dropped — the one
        # failure the classifier is not allowed to have.
        result = classify(
            msg(
                sender='"ISelection [via ALLIBO]" <mail_delivery_service@allibo.com>',
                subject="Candidatura ricevuta",
                precedence="bulk",
            ),
            ctx(),
        )
        assert result.outcome == "pass"

    def test_but_a_bulk_sender_that_is_not_an_ats_still_pays(self) -> None:
        result = classify(
            msg(
                sender="Shop <news@shop.example>",
                subject="La tua candidatura al nostro concorso",
                precedence="bulk",
            ),
            ctx(),
        )
        assert result.score == -2

    def test_the_waiver_does_not_cover_the_rest_of_what_the_platform_sends(self) -> None:
        # 186 messages in a real mailbox, every one of them a review item asking
        # a human to classify "your profile appeared in 8 searches".
        result = classify(
            msg(
                sender="LinkedIn <notifications-noreply@linkedin.com>",
                subject="Il tuo profilo è apparso in 8 ricerche questa settimana",
                list_id="<notifications.linkedin.com>",
                precedence="bulk",
            ),
            ctx(),
        )
        assert result.outcome == "drop"


class TestTheTwoVocabularies:
    def test_a_weak_word_alone_is_not_enough(self) -> None:
        # "la selezione dei nuovi arrivi" from a fashion retailer. Two thirds of
        # everything that reached the ladder in a real mailbox was this.
        result = classify(
            msg(
                sender="FARFETCH <farfetch@emails.farfetch.com>",
                subject="Solo per te: novità",
                text="La selezione dei nuovi arrivi",
            ),
            ctx(),
        )
        assert result.outcome != "pass"

    def test_but_a_weak_word_plus_a_corroborating_signal_is(self) -> None:
        result = classify(
            msg(
                sender="Greenhouse <no-reply@greenhouse-mail.io>",
                subject="La posizione per cui hai scritto",
            ),
            ctx(),
        )
        assert result.outcome == "pass"

    def test_a_strong_word_stands_on_its_own(self) -> None:
        result = classify(
            msg(sender="Giulia <giulia@studio-x.it>", subject="La tua candidatura"),
            ctx(),
        )
        assert result.score >= 2


class TestIdentitySignals:
    def test_a_reply_on_an_owned_thread_is_worth_two_points(self) -> None:
        without = classify(msg(subject="Re: aggiornamento", thread_id="t1"), ctx())
        with_thread = classify(
            msg(subject="Re: aggiornamento", thread_id="t1"), ctx(known_threads={"t1"})
        )
        assert with_thread.score - without.score == 2

    def test_a_meeting_link_from_a_personal_address_does_not_count(self) -> None:
        text = "https://meet.google.com/abc-defg-hij"
        business = classify(msg(sender="HR <hr@nexi.it>", text=text), ctx())
        personal = classify(msg(sender="Mamma <mamma@gmail.com>", text=text), ctx())
        assert business.score - personal.score == 2

    def test_direct_mail_from_a_company_in_the_pipeline_scores_but_bulk_does_not(self) -> None:
        known = ctx(company_domains={"nexi.it"})
        direct = classify(msg(sender="HR <hr@nexi.it>"), known)
        blasted = classify(
            msg(sender="HR <hr@nexi.it>", list_unsubscribe="<mailto:x@nexi.it>"), known
        )
        assert direct.score == 3
        assert blasted.score == 0


class TestPenalties:
    def test_a_no_reply_sender_with_no_vocabulary_is_pushed_down(self) -> None:
        result = classify(msg(sender="no-reply@shop.example", subject="Il tuo ordine"), ctx())
        assert result.score <= -3
        assert result.outcome == "drop"

    def test_but_a_no_reply_that_mentions_an_application_is_not(self) -> None:
        result = classify(
            msg(sender="no-reply@nexi.it", subject="La tua candidatura in Nexi"), ctx()
        )
        assert result.score == 2
        assert result.outcome != "drop"

    def test_developer_notifications_are_pushed_down(self) -> None:
        result = classify(
            msg(sender="GitHub <notifications@github.com>", subject="Security alert"), ctx()
        )
        assert result.outcome == "drop"


class TestTheThreeOutcomes:
    """The classifier's whole job is one of three answers, and the middle one
    is the reason `costly` exists on a rung.
    """

    def test_a_borderline_message_goes_down_the_cheap_rungs_only(self) -> None:
        # `cheap_only` is what stops a maybe from being worth an inference. It
        # was unreachable in Python until rung 3 existed, and untested either
        # way — a flag nothing reads is indistinguishable from a flag that is
        # wrong.
        assert (
            classify(
                msg(
                    sender="someone@unknown-company.example",
                    subject="About the role",
                    text="Following up about the role we discussed.",
                ),
                ctx(),
            ).outcome
            == "cheap_only"
        )

    def test_a_calendar_invite_always_passes(self) -> None:
        # An invitation is the one thing that never needs the vocabulary: a
        # meeting somebody scheduled with you is evidence on its own.
        invited = msg(
            sender="talent@somecompany.example",
            subject="Invitation",
            invite=CalendarInvite(
                uid="ics-1",
                summary="Technical interview",
                starts_at=datetime(2026, 7, 31, 8, 0, tzinfo=UTC),
                organiser="talent@somecompany.example",
            ),
        )
        assert classify(invited, ctx()).outcome != "drop"

    def test_a_newsletter_drops(self) -> None:
        assert (
            classify(
                msg(
                    sender="news@newsletter.example",
                    subject="This week in tech",
                    list_id="weekly",
                    precedence="bulk",
                ),
                ctx(),
            ).outcome
            == "drop"
        )
