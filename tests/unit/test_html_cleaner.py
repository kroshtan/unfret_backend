from unfret.html_cleaner import clean_html


def test_strips_noise_and_keeps_visible_text() -> None:
    html = """
    <html><head><title>t</title><script>var x = 1;</script></head>
    <body>
      <nav>Home | Shop</nav>
      <h1>Merino   Wool   Sweater</h1>
      <p>100% merino.</p>



      <p>Free returns.</p>
      <footer>© Shop</footer>
    </body></html>
    """
    text = clean_html(html)
    assert "Merino Wool Sweater" in text
    assert "100% merino." in text
    assert "Free returns." in text
    for noise in ("var x", "Home | Shop", "© Shop"):
        assert noise not in text
    assert "\n\n\n" not in text


def test_empty_page_returns_empty_string() -> None:
    assert clean_html("<html><script>1</script></html>") == ""
