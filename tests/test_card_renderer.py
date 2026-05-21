"""Tests for card_renderer — nested list rendering and regression coverage."""

import unittest
import html

import card_renderer
from card_renderer import markdown_to_html, build_markdown_card


class TestNestedLists(unittest.TestCase):
    """Nested-list rendering (the main new feature)."""

    def test_flat_unordered(self):
        """A flat unordered list is unchanged."""
        md = "- Alpha\n- Beta\n- Gamma"
        html_out = markdown_to_html(md)
        self.assertIn("<ul>", html_out)
        self.assertIn("<li>Alpha</li>", html_out)
        self.assertIn("<li>Beta</li>", html_out)
        self.assertIn("<li>Gamma</li>", html_out)
        self.assertIn("</ul>", html_out)
        self.assertEqual(html_out.count("<ul>"), 1)
        self.assertEqual(html_out.count("</ul>"), 1)

    def test_nested_unordered(self):
        """Two-level nesting preserves structure."""
        md = "- Parent\n  - Child 1\n  - Child 2\n- Sibling"
        html_out = markdown_to_html(md)
        # Outer list
        self.assertIn("<ul>", html_out)
        self.assertIn("<li>Parent", html_out)
        self.assertIn("<li>Sibling</li>", html_out)
        # Nested list inside Parent <li>
        self.assertIn("<li>Child 1</li>", html_out)
        self.assertIn("<li>Child 2</li>", html_out)
        self.assertEqual(html_out.count("<ul>"), 2)
        self.assertEqual(html_out.count("</ul>"), 2)

    def test_nested_ordered(self):
        """Ordered list with nested ordered list."""
        md = "1. First\n   1. Sub 1\n   2. Sub 2\n2. Second"
        html_out = markdown_to_html(md)
        self.assertIn("<ol>", html_out)
        self.assertIn("<li>First", html_out)
        self.assertIn("<li>Second</li>", html_out)
        self.assertEqual(html_out.count("<ol>"), 2)

    def test_mixed_nesting(self):
        """Unordered list containing nested ordered list."""
        md = "- Item\n  1. Ordered 1\n  1. Ordered 2\n- Another"
        html_out = markdown_to_html(md)
        self.assertIn("<ul>", html_out)
        self.assertIn("<ol>", html_out)
        self.assertIn("<li>Ordered 1</li>", html_out)
        self.assertIn("<li>Ordered 2</li>", html_out)
        # Outer <ul> has 2 items
        self.assertEqual(html_out.count("<ul>"), 1)
        self.assertEqual(html_out.count("<ol>"), 1)

    def test_three_level_nesting(self):
        """Three levels of nesting."""
        md = "- A\n  - B\n    - C\n  - D\n- E"
        html_out = markdown_to_html(md)
        self.assertIn("<li>A", html_out)
        self.assertIn("<li>B", html_out)
        self.assertIn("<li>C</li>", html_out)
        self.assertIn("<li>D</li>", html_out)
        self.assertIn("<li>E</li>", html_out)
        self.assertEqual(html_out.count("<ul>"), 3)
        self.assertEqual(html_out.count("</ul>"), 3)

    def test_inline_formatting_in_nested(self):
        """Inline code/strong/emphasis inside nested items."""
        md = "- `code`\n  - **bold**\n  - *italic*"
        html_out = markdown_to_html(md)
        self.assertIn("<code>code</code>", html_out)
        self.assertIn("<strong>bold</strong>", html_out)
        self.assertIn("<em>italic</em>", html_out)


class TestRegression(unittest.TestCase):
    """Existing features must remain unchanged."""

    def test_empty(self):
        self.assertEqual(markdown_to_html(""), "<p></p>")

    def test_paragraph(self):
        out = markdown_to_html("Hello world")
        self.assertIn("<p>", out)
        self.assertIn("Hello world", out)
        self.assertIn("</p>", out)

    def test_headings(self):
        md = "# H1\n## H2\n### H3"
        out = markdown_to_html(md)
        self.assertIn("<h1>H1</h1>", out)
        self.assertIn("<h2>H2</h2>", out)
        self.assertIn("<h3>H3</h3>", out)

    def test_code_fence(self):
        md = "```\nprint(1)\n```"
        out = markdown_to_html(md)
        self.assertIn("<pre><code>", out)
        self.assertIn("print(1)", out)

    def test_inline_code(self):
        out = markdown_to_html("Use `print()`")
        self.assertIn("<code>print()</code>", out)

    def test_strong_and_emphasis(self):
        out = markdown_to_html("**bold** and *italic*")
        self.assertIn("<strong>bold</strong>", out)
        self.assertIn("<em>italic</em>", out)

    def test_blockquote(self):
        md = "> Quote line"
        out = markdown_to_html(md)
        self.assertIn("<blockquote>", out)
        self.assertIn("Quote line", out)

    def test_table(self):
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        out = markdown_to_html(md)
        self.assertIn("<table>", out)
        self.assertIn("<th>A</th>", out)
        self.assertIn("<td>1</td>", out)

    def test_horizontal_rule(self):
        out = markdown_to_html("---")
        self.assertIn("<hr />", out)

    def test_html_escaping(self):
        out = markdown_to_html("<script>alert(1)</script>")
        self.assertNotIn("<script>", out)
        self.assertIn(html.escape("<script>alert(1)</script>"), out)

    def test_build_markdown_card(self):
        """build_markdown_card returns a CardRenderPayload with expected keys."""
        payload = build_markdown_card("# Hello")
        self.assertEqual(payload.template, card_renderer.CARD_TEMPLATE)
        self.assertIn("Hello", payload.data["body"])
        self.assertEqual(payload.options["type"], "png")


class TestOrderedListVariants(unittest.TestCase):
    """Ordered-list markers (dot and paren)."""

    def test_dot_marker(self):
        out = markdown_to_html("1. One\n2. Two")
        self.assertIn("<ol>", out)
        self.assertIn("<li>One</li>", out)
        self.assertIn("<li>Two</li>", out)

    def test_paren_marker(self):
        out = markdown_to_html("1) One\n2) Two")
        self.assertIn("<ol>", out)
        self.assertIn("<li>One</li>", out)
        self.assertIn("<li>Two</li>", out)


if __name__ == "__main__":
    unittest.main()
