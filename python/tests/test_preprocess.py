from loop.domain.preprocess import (
    detect_language,
    excerpt,
    html_to_text,
    normalise_message,
    strip_quoted_history,
)


class TestHtmlToText:
    def test_drops_anything_that_can_execute_or_phone_home(self) -> None:
        html = (
            "<html><head><style>p{color:red}</style></head><body>"
            "<script>fetch('http://evil')</script>"
            "<p>Grazie per la tua candidatura</p>"
            '<img src="http://tracker/pixel.gif" width="1">'
            "</body></html>"
        )
        text = html_to_text(html)
        assert "fetch" not in text
        assert "color:red" not in text
        assert "pixel.gif" not in text
        assert "Grazie per la tua candidatura" in text

    def test_keeps_the_href_so_a_posting_url_survives(self) -> None:
        # Angle brackets would be removed by the generic tag strip below, which
        # is what happened in the TypeScript.
        text = html_to_text('<a href="https://jobs.nexi.it/postings/42">See the role</a>')
        assert "See the role (https://jobs.nexi.it/postings/42)" in text

    def test_a_link_whose_label_is_the_url_is_not_written_twice(self) -> None:
        text = html_to_text('<a href="https://x.test/a">https://x.test/a</a>')
        assert text.count("https://x.test/a") == 1

    def test_decodes_entities_the_typescript_left_as_literals(self) -> None:
        assert "Bell'Italia" in html_to_text("<p>Bell&rsquo;Italia</p>").replace("’", "'")
        assert "R&D" in html_to_text("<p>R&amp;D</p>")


class TestStripQuotedHistory:
    def test_cuts_at_the_earliest_marker_whichever_language(self) -> None:
        body = (
            "Purtroppo non proseguiremo.\n\n"
            "Il 3 marzo 2026, Michele Bellitti ha scritto:\n"
            "> Buongiorno, allego il CV\n"
        )
        assert strip_quoted_history(body).strip() == "Purtroppo non proseguiremo."

    def test_removes_a_trailing_quote_block_with_no_marker_above_it(self) -> None:
        assert strip_quoted_history("Ciao\n> vecchio messaggio\n> ancora").strip() == "Ciao"

    def test_leaves_a_message_that_quotes_nothing_alone(self) -> None:
        body = "We would like to invite you to an interview."
        assert strip_quoted_history(body) == body


class TestNormaliseMessage:
    def test_collapses_the_whitespace_bulk_mail_is_padded_with(self) -> None:
        # The non-breaking runs in the middle are what a marketing template
        # uses to fake vertical rhythm; left in, they are 3 000 characters of
        # the 6 000 a rung gets to read.
        result = normalise_message(text="La  selezione\t\tdei  nuovi\n\n\n\narrivi")
        assert result.text == "La selezione dei nuovi\n\narrivi"

    def test_caps_the_text_and_says_so(self) -> None:
        result = normalise_message(text="x" * 7000)
        assert result.truncated
        assert len(result.text) == 6000

    def test_collects_links_before_the_cap_because_a_posting_url_sits_late(self) -> None:
        result = normalise_message(
            text=f"{'x' * 6100}\nhttps://careers.example.com/jobs/9 https://careers.example.com/jobs/9"
        )
        assert result.links == ("https://careers.example.com/jobs/9",)
        assert result.truncated


class TestExcerpt:
    def test_returns_short_text_untouched_but_flattened(self) -> None:
        assert excerpt("due   righe\ndi testo") == "due righe di testo"

    def test_breaks_on_a_word_and_marks_the_cut(self) -> None:
        result = excerpt("parola " * 60, limit=40)
        assert len(result) <= 41
        assert result.endswith("…")
        assert not result.rstrip("…").endswith("paro")


class TestDetectLanguage:
    def test_reads_both_languages_and_admits_when_it_is_neither(self) -> None:
        assert detect_language("Grazie per la tua candidatura, cordiali saluti") == "it"
        assert detect_language("Thanks for your application, we have received it") == "en"
        assert detect_language("42") == "other"
