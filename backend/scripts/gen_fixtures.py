"""The synthetic corpus.

§17 wants 150 real messages, anonymised, plus 100 negatives — and it is right
that the real corpus is the project's spine. This generates the *structural*
corpus CI can run on a clean checkout: every ATS vendor, both languages, each
intent, and the negatives that must be dropped. It is what makes the confusion
matrix reproducible for someone who has never seen your inbox.

Your own mail is added on top with `scripts/anonymise.py`, into
`fixtures/private/`, which is git-ignored. The gate that decides whether the
product ships — ≥0.85 application-level recall over twelve real months — can
only be measured there.

    uv run python scripts/gen_fixtures.py

The output is committed, so running this should produce no diff unless a fixture
was added or changed. `--check` asserts exactly that, which is what makes the
generator and the files it generated impossible to drift apart quietly.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from loop.paths import backend_root

DATE = "Thu, 30 Jul 2026 09:12:00 +0200"
RECIPIENT = "you@example.com"


def eml(fixture: dict[str, Any]) -> str:
    """The message as a client would have written it.

    `\r\n` inside an `.ics` and `\n` between headers, because that is what real
    mail looks like: RFC 5545 mandates CRLF in the calendar body and no mail
    client rewrites it on the way into a MIME part.
    """
    lines = [
        f"From: {fixture['from']}",
        f"To: {fixture.get('to', RECIPIENT)}",
        f"Subject: {fixture['subject']}",
        f"Date: {DATE}",
        f"Message-ID: <{fixture['name']}@fixture.loop>",
        *(f"{k}: {v}" for k, v in fixture.get("headers", {}).items()),
    ]

    if fixture.get("ics"):
        lines += [
            "MIME-Version: 1.0",
            'Content-Type: multipart/mixed; boundary="b1"',
            "",
            "--b1",
            "Content-Type: text/plain; charset=utf-8",
            "",
            fixture["body"],
            "",
            "--b1",
            "Content-Type: text/calendar; method=REQUEST; charset=utf-8",
            'Content-Disposition: attachment; filename="invite.ics"',
            "",
            fixture["ics"],
            "",
            "--b1--",
        ]
    else:
        kind = "html" if fixture.get("html") else "plain"
        lines += [f"Content-Type: text/{kind}; charset=utf-8", "", fixture["body"]]

    return "\n".join(lines) + "\n"


def ics(uid: str, summary: str, start: str, organiser: str = "talent@nexi.it") -> str:
    """One hour, CRLF, as RFC 5545 mandates and as every real invitation sends.

    The committed fixtures used to hold LF here, because the generator and its
    output had drifted apart. LF is the wrong shape to test against: the
    connector's `parse_ics` meets CRLF on every real message.
    """
    hour = int(start[9:11])
    end = f"{start[:9]}{hour + 1:02d}{start[11:]}"
    return "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "METHOD:REQUEST",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"SUMMARY:{summary}",
            f"DTSTART:{start}",
            f"DTEND:{end}",
            f"ORGANIZER:mailto:{organiser}",
            "ATTENDEE:mailto:you@example.com",
            "STATUS:CONFIRMED",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )


ATS: list[dict[str, Any]] = [
    {
        "name": "greenhouse-ack-01",
        "expect": {
            "intent": "acknowledged",
            "company": "Zalando",
            "vendor": "greenhouse"
        },
        "from": "no-reply@eu.greenhouse-mail.io",
        "subject": "Thank you for applying to Zalando",
        "body": (
            "Hello,\n\nWe have received your application for the Senior Backend Engineer "
            "position and the team will review it shortly.\n\nThe Zalando Talent Team"
        )
    },
    {
        "name": "greenhouse-reject-01",
        "expect": {
            "intent": "rejected",
            "vendor": "greenhouse"
        },
        "from": "no-reply@greenhouse-mail.io",
        "subject": "Update on your application",
        "body": (
            "Thank you for your interest. After careful review we have decided to move forward "
            "with other candidates for this role."
        )
    },
    {
        "name": "greenhouse-screening-01",
        "expect": {
            "intent": "schedule_screening",
            "company": "Personio",
            "vendor": "greenhouse"
        },
        "from": "no-reply@greenhouse-mail.io",
        "subject": "Personio | Interview availability",
        "body": (
            "Hi,\n\nWe would like to speak with you for the Backend Engineer position. Please "
            "share your availability for the coming week."
        )
    },
    {
        "name": "lever-ack-01",
        "expect": {
            "intent": "acknowledged",
            "company": "Docebo",
            "vendor": "lever"
        },
        "from": "no-reply@hire.lever.co",
        "subject": "Docebo has received your application",
        "body": "Thanks for applying to Docebo. Our team reviews every application."
    },
    {
        "name": "workday-ack-01",
        "expect": {
            "intent": "acknowledged",
            "company": "Nexi",
            "vendor": "workday"
        },
        "from": "nexi@myworkday.com",
        "subject": "Nexi: Application Received",
        "body": (
            "Dear candidate, thank you for your interest in the Platform Engineer position at "
            "Nexi."
        )
    },
    {
        "name": "workday-reject-it-01",
        "expect": {
            "intent": "rejected",
            "vendor": "workday"
        },
        "from": "nexi@myworkday.com",
        "subject": "Aggiornamento sulla tua candidatura",
        "body": (
            "Gentile candidato, la ringraziamo per il tempo dedicato. Abbiamo deciso di "
            "proseguire con altri profili per questa posizione."
        )
    },
    {
        "name": "ashby-ack-01",
        "expect": {
            "intent": "acknowledged",
            "company": "Satispay",
            "vendor": "ashby"
        },
        "from": "notifications@ashbyhq.com",
        "subject": "Satispay — Application Received",
        "body": "We have received your application for Backend Engineer."
    },
    {
        "name": "ashby-takehome-01",
        "expect": {
            "intent": "take_home",
            "vendor": "ashby"
        },
        "from": "notifications@ashbyhq.com",
        "subject": "Next step at Satispay",
        "body": (
            "The next step is a take-home exercise. Please submit your solution by Sunday 3 "
            "August."
        )
    },
    {
        "name": "smartrecruiters-ack-01",
        "expect": {
            "intent": "acknowledged",
            "company": "Sportradar",
            "vendor": "smartrecruiters"
        },
        "from": "no-reply@smartrecruiters.com",
        "subject": "Thank you for applying at Sportradar",
        "body": "Your application has been received."
    },
    {
        "name": "workable-ack-01",
        "expect": {
            "intent": "acknowledged",
            "company": "Prima Assicurazioni",
            "vendor": "workable"
        },
        "from": "no-reply@workablemail.com",
        "subject": "Thank you for applying to Prima Assicurazioni",
        "body": "We have your application for Backend Engineer."
    },
    {
        "name": "icims-ack-01",
        "expect": {
            "intent": "acknowledged",
            "vendor": "icims"
        },
        "from": "careers@talent.icims.com",
        "subject": "Thank you for your interest in our company",
        "body": "Your application has been submitted successfully."
    },
    {
        "name": "taleo-ack-01",
        "expect": {
            "intent": "acknowledged",
            "company": "Iliad Italia",
            "vendor": "taleo"
        },
        "from": "noreply@taleo.net",
        "subject": "Candidatura ricevuta - Iliad Italia",
        "body": "Abbiamo ricevuto la sua candidatura per la posizione di Software Engineer."
    },
    {
        "name": "recruitee-ack-01",
        "expect": {
            "intent": "acknowledged",
            "company": "Translated",
            "vendor": "recruitee"
        },
        "from": "no-reply@mail.recruitee.com",
        "subject": "Translated - Application received",
        "body": "Thanks for applying at Translated."
    },
    {
        "name": "bamboohr-ack-01",
        "expect": {
            "intent": "acknowledged",
            "vendor": "bamboohr"
        },
        "from": "no-reply@mail.bamboohr.com",
        "subject": "Everli Application Confirmation",
        "body": "Thank you for your application."
    },
    {
        "name": "linkedin-applied-01",
        "expect": {
            "intent": "applied",
            "company": "Nexi",
            "vendor": "linkedin"
        },
        "from": "jobs-noreply@linkedin.com",
        "subject": "Your application was sent to Nexi",
        "headers": {
            "List-Unsubscribe": "<https://www.linkedin.com/unsubscribe>",
            "List-Id": "jobs.linkedin.com",
            "Precedence": "bulk"
        },
        "body": "Your application for Platform Engineer was sent to Nexi."
    },
    {
        "name": "linkedin-applied-it-01",
        "expect": {
            "intent": "applied",
            "company": "Casavo",
            "vendor": "linkedin"
        },
        "from": "jobs-noreply@linkedin.com",
        "subject": "La tua candidatura è stata inviata a Casavo",
        "headers": {
            "List-Unsubscribe": "<https://www.linkedin.com/unsubscribe>",
            "Precedence": "bulk"
        },
        "body": "La tua candidatura per Senior Engineer è stata inviata a Casavo."
    },
    {
        "name": "indeed-applied-01",
        "expect": {
            "intent": "applied",
            "company": "Everli",
            "vendor": "indeed"
        },
        "from": "noreply@indeedemail.com",
        "subject": "Indeed Application: Backend Engineer - Everli",
        "headers": {
            "List-Id": "indeed-apply",
            "Precedence": "bulk"
        },
        "body": "You applied to Backend Engineer at Everli."
    },
    {
        "name": "calendar-invite-01",
        "expect": {
            "intent": "interview_invite"
        },
        "from": "Marta <talent@nexi.it>",
        "subject": "Invitation: System & code review @ Fri 31 Jul",
        "body": "Looking forward to it.",
        "ics": ics("nexi-round-2@nexi.it", "System & code review", "20260731T100000Z")
    },
    {
        "name": "calendar-invite-hr-01",
        "expect": {
            "intent": "interview_invite"
        },
        "from": "People <people@satispay.com>",
        "subject": "Invitation: Intro call",
        "body": "A short introductory call.",
        "ics": ics(
            "satispay-intro@satispay.com", "HR screening call", "20260801T090000Z"
        )
    },
    {
        "name": "human-offer-01",
        "expect": {
            "intent": "offer",
            "requires_model": True
        },
        "from": "Giulia <giulia@bendingspoons.com>",
        "subject": "Offer — Bending Spoons",
        "body": (
            "Hi,\n\nWe would like to offer you the Software Engineer role at €68,000 base plus "
            "equity. Could you let us know by 8 August?\n\nGiulia"
        )
    },
    {
        "name": "human-italian-reject-01",
        "expect": {
            "intent": "rejected",
            "requires_model": True
        },
        "from": "hr@iliad.it",
        "subject": "La tua candidatura",
        "body": (
            "Ti terremo in considerazione per future opportunità. Grazie ancora per il tempo "
            "dedicato."
        )
    }
]

NEGATIVES: list[dict[str, Any]] = [
    {
        "name": "newsletter-01",
        "expect": {
            "drop": True
        },
        "from": "news@techweekly.example.com",
        "subject": "This week in engineering",
        "headers": {
            "List-Id": "techweekly",
            "List-Unsubscribe": "<https://example.com/u>",
            "Precedence": "bulk"
        },
        "body": "The five best articles about distributed systems this week."
    },
    {
        "name": "github-01",
        "expect": {
            "drop": True
        },
        "from": "notifications@github.com",
        "subject": "[acme/repo] Pull request merged (#412)",
        "headers": {
            "List-Id": "acme/repo",
            "Precedence": "bulk"
        },
        "body": "The pull request was merged into main."
    },
    {
        "name": "social-01",
        "expect": {
            "drop": True
        },
        "from": "notification@facebookmail.com",
        "subject": "You have 3 new notifications",
        "headers": {
            "List-Unsubscribe": "<https://facebook.com/u>",
            "Precedence": "bulk"
        },
        "body": "See what your friends have been posting."
    },
    {
        "name": "invoice-01",
        "expect": {
            "drop": True
        },
        "from": "billing@hosting.example.com",
        "subject": "Your invoice for July",
        "headers": {
            "Precedence": "bulk",
            "List-Id": "billing"
        },
        "body": "Your invoice is attached. No action is required."
    },
    {
        "name": "linkedin-jobalert-01",
        "expect": {
            "drop": False
        },
        "from": "jobalerts-noreply@linkedin.com",
        "subject": "12 new jobs for Backend Engineer",
        "headers": {
            "List-Id": "jobs.linkedin.com",
            "Precedence": "bulk"
        },
        "body": "Here are jobs that match your search."
    },
    {
        "name": "calendar-personal-01",
        "expect": {
            "drop": True
        },
        "from": "mum@gmail.com",
        "subject": "Dinner Sunday",
        "body": "Are you free on Sunday evening?"
    },
    {
        "name": "marketing-01",
        "expect": {
            "drop": True
        },
        "from": "offers@shop.example.com",
        "subject": "Summer sale — 40% off everything",
        "headers": {
            "Precedence": "bulk",
            "List-Unsubscribe": "<https://shop.example.com/u>"
        },
        "body": "Shop the sale before it ends."
    },
    {
        "name": "security-alert-01",
        "expect": {
            "drop": True
        },
        "from": "no-reply@accounts.google.com",
        "subject": "Security alert",
        "body": "A new device signed in to your account."
    }
]


def generate() -> tuple[dict[Path, str], list[dict[str, Any]]]:
    """Every file this corpus is, and the manifest that indexes it."""
    files: dict[Path, str] = {}
    manifest: list[dict[str, Any]] = []
    for directory, fixtures in (("ats", ATS), ("negatives", NEGATIVES)):
        for fixture in fixtures:
            relative = f"fixtures/{directory}/{fixture['name']}.eml"
            files[Path(relative)] = eml(fixture)
            manifest.append({"file": relative, "expect": fixture["expect"]})
    return files, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the committed corpus differs from what this would write",
    )
    args = parser.parse_args()

    root = backend_root()
    files, manifest = generate()
    manifest_text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"

    if args.check:
        # Bytes, not text. An `.ics` part is CRLF and Python's universal-newline
        # reader silently turns it into LF, so a text comparison here reports a
        # drift that is not there — and would hide one that is.
        drifted = [
            str(path)
            for path, text in files.items()
            if not (root / path).is_file()
            or (root / path).read_bytes() != text.encode("utf-8")
        ]
        manifest_path = root / "fixtures" / "manifest.json"
        if manifest_path.read_bytes() != manifest_text.encode("utf-8"):
            drifted.append("fixtures/manifest.json")
        if drifted:
            print("  the committed corpus differs from the generator:", file=sys.stderr)
            for path in drifted:
                print(f"  · {path}", file=sys.stderr)
            raise SystemExit(1)
        print(f"corpus matches the generator ({len(files)} files)")
        return

    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    (root / "fixtures" / "manifest.json").write_bytes(manifest_text.encode("utf-8"))
    print(f"wrote {len(ATS)} ATS fixtures and {len(NEGATIVES)} negatives")


if __name__ == "__main__":
    main()
