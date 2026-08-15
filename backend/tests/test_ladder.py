from datetime import UTC, datetime

import pytest
from conftest import LADDER_NOW, candidate_message

from loop.domain.messages import CalendarInvite, CandidateMessage
from loop.ladder import (
    Extracted,
    Ignored,
    LadderContext,
    NeedsReview,
    RuleRegistry,
    deterministic_ladder,
    stage_for_intent,
    stage_from_title,
)
from loop.ladder.company import (
    company_from_display_name,
    company_from_domain,
    company_from_sender,
    is_the_senders_own_name,
)
from loop.ladder.role import role_from_body

REGISTRY = RuleRegistry.load()
NOW = LADDER_NOW

candidate = candidate_message


def read(msg: CandidateMessage, **over: object) -> Extracted | NeedsReview | Ignored:
    ctx = LadderContext(registry=REGISTRY, **over)  # type: ignore[arg-type]
    return deterministic_ladder().run(msg, ctx)


def signal_of(msg: CandidateMessage, **over: object):
    outcome = read(msg, **over)
    assert isinstance(outcome, Extracted)
    return outcome.signal


class TestRungOne:
    def test_reads_the_employer_from_the_display_name_not_the_subject(self) -> None:
        # "Thanks for applying to Machine Learning Engineer, here is a link to
        # manage your application data" — the slot after "to" holds the role.
        signal = signal_of(
            candidate(
                sender="Prima <no-reply@hire.eu.lever.co>",
                subject=(
                    "Thanks for applying to Machine Learning Engineer , here is a link to "
                    "manage your application data"
                ),
            )
        )
        assert signal.company == "Prima"
        assert signal.role == "Machine Learning Engineer"
        assert signal.rung == 1
        assert signal.ats_vendor == "lever"

    def test_falls_through_to_the_shared_vocabulary_when_no_template_fits(self) -> None:
        # Ashby delivers rejections written in Italian by an Italian company;
        # an English-only rule file never sees them.
        signal = signal_of(
            candidate(
                sender="Lexroom Hiring Team <no-reply@ashbyhq.com>",
                subject="Aggiornamento",
                text="Purtroppo non proseguiremo con la tua candidatura.",
            )
        )
        assert signal.intent == "rejected"
        assert signal.company == "Lexroom"

    def test_abstains_on_a_vendor_whose_mail_it_cannot_read(self) -> None:
        outcome = read(
            candidate(sender="Greenhouse <no-reply@greenhouse-mail.io>", subject="Ciao")
        )
        assert isinstance(outcome, NeedsReview)
        assert outcome.intent is None

    def test_a_lookalike_domain_is_not_the_vendor(self) -> None:
        outcome = read(
            candidate(
                sender="X <no-reply@notgreenhouse-mail.io>",
                subject="Thank you for your application",
            )
        )
        # It may still be read by rung 2, but never as Greenhouse.
        vendor = outcome.signal.ats_vendor if isinstance(outcome, Extracted) else None
        assert vendor is None

    def test_marks_a_vendors_marketing_as_other_rather_than_guessing(self) -> None:
        signal = signal_of(
            candidate(sender="Lever <no-reply@lever.co>", subject="Webinar: hiring in 2026")
        )
        assert signal.intent == "other"


class TestRungTwo:
    def test_an_invite_places_the_interview_at_the_meeting_not_the_mail(self) -> None:
        starts = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
        signal = signal_of(
            candidate(
                sender="Giulia <giulia@nexi.it>",
                subject="Invito",
                invite=CalendarInvite(
                    uid="ev1",
                    summary="System design interview",
                    starts_at=starts,
                    organiser="giulia@nexi.it",
                ),
            )
        )
        assert signal.intent == "interview_invite"
        assert signal.stage_hint == "system_design"
        assert signal.occurred_at == starts
        assert signal.company == "Nexi"

    def test_an_untitled_invite_abstains_instead_of_calling_it_technical(self) -> None:
        signal = signal_of(
            candidate(
                sender="Giulia <giulia@nexi.it>",
                subject="Invito",
                invite=CalendarInvite(uid="ev1", summary="Chat", starts_at=NOW),
            )
        )
        assert signal.intent == "interview_invite"
        assert signal.stage_hint is None

    def test_a_cancellation_is_read_as_one(self) -> None:
        signal = signal_of(
            candidate(
                sender="Giulia <giulia@nexi.it>",
                subject="Annullato",
                invite=CalendarInvite(
                    uid="ev1", summary="HR screening", starts_at=NOW, method="CANCEL"
                ),
            )
        )
        assert signal.intent == "interview_cancelled"

    def test_reads_a_rejection_arriving_on_a_thread_it_already_owns(self) -> None:
        # The TypeScript asserted thread identity as though it were an
        # extraction and returned early, so this became a review item.
        signal = signal_of(
            candidate(
                sender="Giulia <giulia@nexi.it>",
                subject="Re: colloquio",
                text="Purtroppo non proseguiremo.",
                thread_id="t1",
            ),
            thread_to_application={"t1": "app-1"},
        )
        assert signal.intent == "rejected"
        assert signal.application_hint == "app-1"

    def test_identity_is_inherited_whichever_rung_read_the_message(self) -> None:
        signal = signal_of(
            candidate(
                sender="Prima <no-reply@hire.eu.lever.co>",
                subject="Thank you for your application to Prima",
                thread_id="t1",
            ),
            thread_to_application={"t1": "app-1"},
        )
        assert signal.rung == 1
        assert signal.application_hint == "app-1"

    def test_a_practice_site_selling_a_coding_challenge_is_not_a_take_home(self) -> None:
        outcome = read(
            candidate(
                sender="LeetCode <no-reply@leetcode.com>",
                subject="Weekly coding challenge",
                text="Join this week's coding challenge and climb the leaderboard.",
            )
        )
        assert isinstance(outcome, NeedsReview)

    def test_the_same_words_from_a_real_sender_are(self) -> None:
        signal = signal_of(
            candidate(
                sender="Giulia <giulia@nexi.it>",
                subject="Prossimo passo",
                text="Ti inviamo la prova tecnica da completare entro venerdì.",
            )
        )
        assert signal.intent == "take_home"
        assert signal.confidence <= 0.88


class TestTheLadder:
    def test_a_cheap_only_message_never_reaches_a_costly_rung(self) -> None:
        class Expensive:
            costly = True

            def extract(self, msg, ctx):
                raise AssertionError("a cheap_only message must not pay for this")

        from loop.ladder import Ladder

        outcome = Ladder([Expensive()]).run(
            candidate(sender="x@y.test", cheap_only=True), LadderContext(registry=REGISTRY)
        )
        assert isinstance(outcome, NeedsReview)

    def test_a_low_confidence_reading_goes_to_a_human_rather_than_down_the_ladder(self) -> None:
        from loop.ladder import Extraction, Ladder

        class Unsure:
            costly = False

            def extract(self, msg, ctx):
                return Extraction(intent="offer", confidence=0.4, rung=1)

        class Certain:
            costly = False

            def extract(self, msg, ctx):
                raise AssertionError("the first rung that does not abstain wins")

        outcome = Ladder([Unsure(), Certain()]).run(
            candidate(sender="x@y.test"), LadderContext(registry=REGISTRY)
        )
        assert isinstance(outcome, NeedsReview)
        assert outcome.intent == "offer"
        assert outcome.confidence == pytest.approx(0.4)


class TestArrangingTheFirstCall:
    """The intent the vocabulary had no phrase for at all.

    Ten of the twenty-four signals a recall audit found missing were this: an
    Italian recruiter negotiates the whole first call in prose, over several
    short messages, none of which contain the word "colloquio".
    """

    def test_reads_a_confirmed_call(self) -> None:
        signal = signal_of(
            candidate(
                sender="Alice Doro <alice.doro@dinova.one>",
                subject="Re: Opportunità Dinova - AI ENGINEER (Interacta)",
                text="Ti confermo allora la nostra breve chiamata per questo pomeriggio "
                "alle ore 17:00.",
            )
        )
        assert signal.intent == "schedule_screening"
        assert signal.company == "Dinova"
        # The company sits before the dash in the subject and the role after it.
        assert signal.role == "AI ENGINEER"

    def test_reads_a_proposed_time(self) -> None:
        signal = signal_of(
            candidate(
                sender="Federica <federica@enginium.eu>",
                subject="Opportunità lavorativa",
                text="Gentile Michele, va bene. Potrebbe andare bene domani 28/05 alle ore 10?",
            )
        )
        assert signal.intent == "schedule_screening"

    def test_needs_both_a_day_and_a_time_before_it_believes_one(self) -> None:
        # A date on its own is any email ever written.
        outcome = read(
            candidate(
                sender="Newsletter <news@example.test>",
                subject="Aggiornamento",
                text="Ci vediamo domani per il webinar.",
            )
        )
        assert isinstance(outcome, NeedsReview)

    def test_an_italian_acknowledgement_the_vocabulary_did_not_have(self) -> None:
        signal = signal_of(
            candidate(
                sender="Gruppo Maggioli <no-reply.selezione@gruppomaggioli.it>",
                subject="Conferma di registrazione annuncio Gruppo Maggioli",
                text="Grazie per l'interesse dimostrato per la posizione AI Engineer - "
                "Interacta.\n\nTi confermiamo di aver ricevuto la tua candidatura.",
            )
        )
        assert signal.intent == "acknowledged"
        assert signal.company == "Gruppo Maggioli"
        assert signal.role == "AI Engineer"


class TestTheInMailRelay:
    """LinkedIn's relay is the one sender whose display name is not the employer.

    An ATS sends on behalf of the employer and puts the employer in the display
    name, which is why that is the default place to read it. The InMail relay
    does the opposite: the display name is the recruiter who typed the message.
    """

    HEADER = "Federica Pettinato <hit-reply@linkedin.com>"

    def test_never_files_the_recruiter_as_the_employer(self) -> None:
        assert company_from_sender(self.HEADER, REGISTRY.ats_domains) is None

    def test_but_still_reads_what_the_message_says(self) -> None:
        signal = signal_of(
            candidate(
                sender=self.HEADER,
                subject="Risposta al messaggio: Opportunità lavorativa",
                text="Sì certo. Allora ti contatterò domani alle ore 10:15.",
            )
        )
        assert signal.intent == "schedule_screening"
        # No employer is the right answer: the company is named in the InMail
        # that opened the thread, which is the resolver's to carry forward.
        assert signal.company is None

    def test_a_vendor_that_does_send_as_the_employer_is_unaffected(self) -> None:
        assert (
            company_from_sender("Prima <no-reply@hire.eu.lever.co>", REGISTRY.ats_domains)
            == "Prima"
        )


class TestSelfSentMail:
    OWN = frozenset({"michele@gmail.com"})

    def test_a_message_you_wrote_asserts_nothing_about_the_employer(self) -> None:
        # "I am happy to accept the invitation to the next step" reads as an
        # invitation. It is you accepting one.
        outcome = read(
            candidate(
                sender="Michele Bellitti <michele@gmail.com>",
                subject="Re: Machine Learning Engineer",
                text="Thank you for the invitation. I would like to invite you to pick a slot.",
                thread_id="t1",
            ),
            own_addresses=self.OWN,
            thread_to_application={"t1": "app-1"},
        )
        assert isinstance(outcome, Ignored)

    def test_and_is_not_a_review_item_either(self) -> None:
        # There is nothing for a human to decide about their own email.
        outcome = read(
            candidate(sender="michele@gmail.com", subject="Re: colloquio", text="Grazie mille"),
            own_addresses=self.OWN,
        )
        assert isinstance(outcome, Ignored)
        assert not isinstance(outcome, NeedsReview)

    def test_the_same_thread_read_from_their_side_is_still_extracted(self) -> None:
        signal = signal_of(
            candidate(
                sender="Clara Villamayor <clara.villamayor@prima.it>",
                subject="Machine Learning Engineer",
                text="We would like to invite you to the next step of the selection.",
                thread_id="t1",
            ),
            own_addresses=self.OWN,
            thread_to_application={"t1": "app-1"},
        )
        assert signal.intent == "interview_invite"


class TestReaders:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("Lexroom Hiring Team <no-reply@ashbyhq.com>", "Lexroom"),
            ("Air Apps Recruiting <x@y.test>", "Air Apps"),
            ("Prima via Lever <x@y.test>", "Prima"),
            ("Careers @ Jet <x@y.test>", "Jet"),
            ("noreplyHRrecruitingTeam <x@y.test>", None),
            ("no-reply <x@y.test>", None),
            ("bare@address.test", None),
        ],
    )
    def test_the_employer_out_of_a_from_display_name(
        self, header: str, expected: str | None
    ) -> None:
        assert company_from_display_name(header) == expected

    def test_the_domain_vetoes_a_suffix_strip_that_disagrees_with_it(self) -> None:
        # "Careers @ Jet HR" reduces to "Jet" once the prefix and then `hr` come
        # off, but the mail is from jethr.com and the company is called Jet HR.
        # The suffix list cannot tell a recruiting team from a company whose
        # name contains the same word; the domain can.
        assert (
            company_from_display_name('"Careers @ Jet HR" <careers-noreply@jethr.com>')
            == "Jet HR"
        )
        # And it does not veto a strip the domain has no opinion about.
        assert (
            company_from_display_name("Lexroom Hiring Team <no-reply@ashbyhq.com>") == "Lexroom"
        )

    def test_a_recruiters_own_name_resolves_to_the_company_she_writes_for(self) -> None:
        # Otherwise the same person yields "Prima" through her calendar invites
        # and "Clara Villamayor" through her email, and the resolver builds two
        # applications for one job.
        header = "Clara Villamayor <clara.villamayor@prima.it>"
        assert is_the_senders_own_name(header)
        assert company_from_sender(header) == "Prima"

    def test_a_person_at_a_personal_address_names_no_company_at_all(self) -> None:
        assert company_from_sender("Michele Bellitti <michelebellitti272@gmail.com>") is None

    def test_a_display_name_that_is_not_the_address_is_still_believed(self) -> None:
        assert not is_the_senders_own_name("Prima <no-reply@hire.eu.lever.co>")
        assert company_from_sender("Prima <no-reply@hire.eu.lever.co>") == "Prima"

    def test_strips_a_bracketed_vendor_the_same_as_an_unbracketed_one(self) -> None:
        header = '"ISelection - Iagica srl [via ALLIBO]" <mail_delivery_service@allibo.com>'
        assert company_from_display_name(header) == "ISelection - Iagica srl"

    def test_a_bare_domain_yields_a_company_where_the_typescript_gave_up(self) -> None:
        assert company_from_domain("talent.nexi.it", REGISTRY.ats_domains) == "Nexi"
        assert company_from_domain("giulia@bending-spoons.com", ()) == "Bending Spoons"

    def test_but_never_from_an_ats_a_mailbox_provider_or_a_meeting_host(self) -> None:
        assert company_from_domain("hire.eu.lever.co", REGISTRY.ats_domains) is None
        assert company_from_domain("someone@gmail.com", ()) is None
        assert company_from_domain("meet.google.com", ()) is None

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("Your application for the Backend Engineer position", "Backend Engineer"),
            ("Candidatura per la posizione di Data Scientist presso Nexi", "Data Scientist"),
            ("Position: Senior Platform Engineer", "Senior Platform Engineer"),
            # A sentence that is not a job title.
            ("Thanks for applying to here is a link to manage your data", None),
            # Long enough to be prose rather than a title.
            ("Position of head of everything that moves in the whole company", None),
        ],
    )
    def test_the_job_title_out_of_a_body(self, body: str, expected: str | None) -> None:
        assert role_from_body(body) == expected

    def test_a_calendar_title_names_its_own_stage_and_admits_when_it_does_not(self) -> None:
        assert stage_from_title("Final round with the CTO") == "final"
        assert stage_from_title("Live coding") == "technical"
        assert stage_from_title("HR screening") == "hr_call"
        assert stage_from_title("Chiacchierata") is None
        assert stage_from_title(None) is None

    def test_an_intent_implies_a_stage_except_when_it_does_not(self) -> None:
        assert stage_for_intent("acknowledged") == "acknowledged"
        assert stage_for_intent("take_home") == "take_home"
        # The claim an invitation supports is the phase, not the round.
        assert stage_for_intent("interview_invite") is None
