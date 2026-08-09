"""Unit tests for the context_layout doc-layout SSOT (low/max)."""

from ouroboros import context_layout as cl


def test_tier0_protected_core_declared():
    """The protected always-full core is a data invariant; future context-mode
    work must not silently demote any of these (BIBLE P1 / P4)."""
    expected = {
        "system",
        "bible",
        "identity",
        "scratchpad",
        "knowledge_index",
        "recent_dialogue",
    }
    assert expected <= set(cl.TIER0_ALWAYS_FULL)


def test_nav_map_lists_headings_with_line_ranges_and_omits_body():
    text = (
        "# Title\n\nintro\n\n## Alpha\n\nbody-alpha BODYSENT\n\n"
        "### Sub one\n\nx\n\n## Beta\n\nbody-beta\n"
    )
    m = cl.generate_doc_nav_map(text, title="ARCHITECTURE.md", rel_path="docs/ARCHITECTURE.md")
    assert "navigation map" in m
    assert "read_file" in m  # tells the agent how to pull full sections
    assert 'root="system_repo"' in m
    assert "Alpha" in m and "Beta" in m and "Sub one" in m
    assert "lines" in m
    # Structure only — the section bodies are NOT inlined.
    assert "BODYSENT" not in m
    assert "body-beta" not in m


def test_nav_map_is_fence_aware():
    """A '## ' line inside a code fence must not be parsed as a heading."""
    text = "## Real\n\n```\n## fake-heading-in-fence\n```\n\n## Real2\n"
    m = cl.generate_doc_nav_map(text, title="X", rel_path="x.md")
    assert "Real" in m and "Real2" in m
    assert "fake-heading-in-fence" not in m


def test_reference_doc_sections_decouple_arch_mode_from_dev_inclusion():
    """D-ARCH (owner, 2026-08-08): context_mode decides ONLY the ARCHITECTURE
    form (full in max, nav map in low); DEVELOPMENT inclusion is the caller's
    mode-independent decision. Whatever is not inlined is named in the visible
    on-demand pointer (P1)."""
    arch = "## Arch A\n\nARCHBODY\n"
    dev = "## Dev A\n\nDEVBODY\n"

    def _render(mode, include_dev):
        parts = cl.reference_doc_sections(
            None,
            context_mode=mode,
            include_development=include_dev,
            architecture_text=arch,
            development_text=dev,
        )
        return "\n\n".join(parts)

    max_no_dev = _render("max", False)
    assert "ARCHBODY" in max_no_dev  # ARCH full in max even without dev context
    assert "DEVBODY" not in max_no_dev
    assert "docs/DEVELOPMENT.md" in max_no_dev  # pointer, never silent

    max_dev = _render("max", True)
    assert "ARCHBODY" in max_dev and "DEVBODY" in max_dev

    low_dev = _render("low", True)
    assert "ARCHBODY" not in low_dev  # nav map in low
    assert "navigation map" in low_dev
    assert "DEVBODY" in low_dev  # DEV inclusion independent of the mode

    low_no_dev = _render("low", False)
    assert "DEVBODY" not in low_no_dev
    assert "docs/DEVELOPMENT.md" in low_no_dev
