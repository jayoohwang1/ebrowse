"""End-to-end action-verb tests: CLI -> daemon -> chromium -> diff output."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixture_server import FixtureServer

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def server():
    with FixtureServer() as srv:
        yield srv


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    home = tmp_path_factory.mktemp("ebrowse_home_actions")
    real_browsers = Path(os.environ.get("HOME", "~")).expanduser() / ".cache" / "ms-playwright"
    e = os.environ.copy()
    e.update(
        {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_RUNTIME_DIR": str(home / ".run"),
            "PLAYWRIGHT_BROWSERS_PATH": os.environ.get(
                "PLAYWRIGHT_BROWSERS_PATH", str(real_browsers)
            ),
            "EBROWSE_SUMMARIZER_ENABLED": "false",
        }
    )
    (home / ".run").mkdir()
    yield e
    subprocess.run(
        [sys.executable, "-m", "ebrowse.cli.main", "daemon", "stop"],
        env=e,
        capture_output=True,
        timeout=15,
    )


def ebrowse(env, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ebrowse.cli.main", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def ref_for(env, sid: str, pattern: str) -> str:
    """Find the ref of the element whose rendered line matches `pattern`."""
    out = ebrowse(env, "expand", sid, "--all").stdout
    m = re.search(pattern + r"[^)\]]*\((@e\d+)", out)
    assert m, f"no element matching {pattern!r} in {sid}:\n{out}"
    return m.group(1)


def test_dropdown_click_reveals_options(server, env):
    r = ebrowse(env, "open", server.url("dropdown.html"))
    assert r.returncode == 0, r.stderr
    btn = ref_for(env, "s2", r"Sort by: Relevance")
    r = ebrowse(env, "click", btn)
    assert r.returncode == 0, r.stderr
    assert "→ partial change" in r.stdout
    assert "Price: high to low" in r.stdout  # revealed option with a fresh ref
    assert re.search(r"~ @e\d+ expanded: \"false\" → \"true\"", r.stdout)


def test_click_revealed_option(server, env):
    opt = ref_for(env, "s2", r"Average rating")
    r = ebrowse(env, "click", opt)
    assert r.returncode == 0, r.stderr
    assert "→ partial change" in r.stdout


def test_native_select(server, env):
    sel = ref_for(env, "s2", r"Results per page")
    r = ebrowse(env, "select", sel, "50")
    assert r.returncode == 0, r.stderr
    assert 'value: "25" → "50"' in r.stdout


def test_form_fill_and_submit_quotes_new_text(server, env):
    ebrowse(env, "open", server.url("form.html"))
    for pattern, value in [
        (r"Full name", "Jayoo"),
        (r"Email address", "jay@example.com"),
        (r"Password", "hunter2hunter2"),
    ]:
        ref = ref_for(env, "s2", rf"\[{pattern}")
        r = ebrowse(env, "fill", ref, value)
        assert r.returncode == 0, r.stderr
        assert "value:" in r.stdout
    # password value is masked, never echoed
    assert "hunter2hunter2" not in ebrowse(env, "expand", "s2").stdout
    tos = ref_for(env, "s2", r"I agree to the")
    assert ebrowse(env, "check", tos).returncode == 0
    submit = ref_for(env, "s2", r"\[Create account")
    r = ebrowse(env, "click", submit)
    assert r.returncode == 0, r.stderr
    assert "Account created!" in r.stdout


def test_native_alert_auto_accepted_with_note(server, env):
    ebrowse(env, "open", server.url("dialogs.html"))
    r = ebrowse(env, "click", "#alert-btn")
    assert r.returncode == 0, r.stderr
    assert "note: native alert auto-accepted" in r.stdout
    assert "Alert was shown." in r.stdout


def test_native_confirm_blocks_page_then_dismiss(server, env):
    ebrowse(env, "open", server.url("dialogs.html"))
    r = ebrowse(env, "click", "#confirm-btn")
    assert r.returncode == 0, r.stderr
    assert "→ dialog opened (blocking)" in r.stdout
    assert 'native confirm: "Really delete this item?"' in r.stdout
    # page-touching verbs are refused with a recovery hint while it's pending
    r = ebrowse(env, "outline")
    assert r.returncode == 1
    assert "confirm dialog is blocking" in r.stderr
    assert "dialog accept" in r.stderr and "dialog dismiss" in r.stderr
    # status reports the pending dialog without resolving it
    r = ebrowse(env, "dialog", "status")
    assert r.returncode == 0, r.stderr
    assert 'confirm dialog: "Really delete this item?"' in r.stdout
    # dismiss resolves it and emits the opening action's diff (confirm → false)
    r = ebrowse(env, "dialog", "dismiss")
    assert r.returncode == 0, r.stderr
    assert "dismissed confirm dialog" in r.stdout
    assert "Deletion cancelled." in r.stdout
    # the resolved diff must not carry the now-stale "resolve it" note
    assert "opened (blocking)" not in r.stdout
    # page is unblocked again
    assert ebrowse(env, "outline").returncode == 0


def test_native_confirm_accept(server, env):
    ebrowse(env, "open", server.url("dialogs.html"))
    ebrowse(env, "click", "#confirm-btn")
    r = ebrowse(env, "dialog", "accept")
    assert r.returncode == 0, r.stderr
    assert "accepted confirm dialog" in r.stdout
    assert "Item deleted." in r.stdout


def test_native_prompt_accept_with_text(server, env):
    ebrowse(env, "open", server.url("dialogs.html"))
    r = ebrowse(env, "click", "#prompt-btn")
    assert r.returncode == 0, r.stderr
    assert "→ dialog opened (blocking)" in r.stdout
    assert "native prompt:" in r.stdout
    r = ebrowse(env, "dialog", "accept", "Beans")
    assert r.returncode == 0, r.stderr
    assert 'accepted prompt dialog with "Beans"' in r.stdout
    assert "Renamed to Beans." in r.stdout


def test_occluded_click_blocked(server, env):
    ebrowse(env, "click", "#modal-btn")
    r = ebrowse(env, "click", "#covered-btn")
    assert r.returncode == 1
    assert "covered by" in r.stderr
    # the cover itself is an anonymous backdrop, but the diagnosis names the
    # open dialog as the thing to resolve
    assert "dialog is open" in r.stderr and "cookie consent" in r.stderr.lower()
    # dismiss the modal, then the click goes through
    r = ebrowse(env, "click", "#accept-cookies")
    assert r.returncode == 0
    r = ebrowse(env, "click", "#covered-btn")
    assert r.returncode == 0, r.stderr
    assert "Purchase started." in r.stdout


def test_blocked_click_names_exposed_cover_ref(server, env):
    # the covering promo banner has a clickable signal, so it has a ref of its
    # own — the blocked error must name it as the executable next step
    ebrowse(env, "open", server.url("covers.html"))
    buy_a = ref_for(env, "s2", r"\[Buy plan A")
    r = ebrowse(env, "click", buy_a)
    assert r.returncode == 1
    assert "covered by" in r.stderr
    m = re.search(r"dismiss or interact with (@e\d+)", r.stderr)
    assert m, r.stderr
    # following the recovery action unblocks the original click
    assert ebrowse(env, "click", m.group(1)).returncode == 0
    r = ebrowse(env, "click", buy_a)
    assert r.returncode == 0, r.stderr
    assert "Purchased plan A." in r.stdout


def test_diagnose_reports_blocker_and_pass(server, env):
    # read-only diagnosis: blocked target names the cover's ref without acting
    ebrowse(env, "open", server.url("covers.html"))  # fresh covers
    buy_a = ref_for(env, "s2", r"\[Buy plan A")
    r = ebrowse(env, "diagnose", buy_a)
    assert r.returncode == 0, r.stderr
    assert "actionability: BLOCKED" in r.stdout
    assert re.search(r"dismiss or interact with @e\d+", r.stdout)
    # nothing was clicked: the promo banner is still there
    r = ebrowse(env, "diagnose", buy_a)
    assert "actionability: BLOCKED" in r.stdout
    # an uncovered control diagnoses as PASS
    nav = ref_for(env, "s1", r"\[Products")
    r = ebrowse(env, "diagnose", nav)
    assert r.returncode == 0, r.stderr
    assert "actionability: PASS" in r.stdout


def test_keyboard_fallback_on_covered_native_button(server, env):
    # a native button under an anonymous non-modal cover: the pointer route is
    # blocked, so the click falls back to trusted focus + Enter (what a
    # keyboard user would do), disclosed in the diff notes
    ebrowse(env, "open", server.url("covers.html"))
    buy_c = ref_for(env, "s2", r"\[Buy plan C")
    r = ebrowse(env, "click", buy_c)
    assert r.returncode == 0, r.stderr
    assert "Purchased plan C." in r.stdout
    assert "note: pointer route blocked" in r.stdout
    assert "activated via keyboard" in r.stdout


def test_blocked_click_honest_about_unexposed_cover(server, env):
    # an anonymous overlay with no clickable signal has no ref; the error must
    # say so instead of pointing at an invisible node, and suggest recovery
    buy_b = ref_for(env, "s2", r"\[Buy plan B")
    r = ebrowse(env, "click", buy_b)
    assert r.returncode == 1
    assert "no exposed ref" in r.stderr
    assert "press Escape" in r.stderr
    assert ebrowse(env, "press", "Escape").returncode == 0
    r = ebrowse(env, "click", buy_b)
    assert r.returncode == 0, r.stderr
    assert "partial change" in r.stdout  # the previously-blocked click now lands


def test_candidate_widgets_discovered_and_clickable(server, env):
    # signal-less custom widgets (addEventListener-only, tabindex, role-less
    # aria state) get '?'-marked candidate refs in expand and are clickable;
    # the zero-signal decoy stays ref-less
    ebrowse(env, "open", server.url("custom_widgets.html"))
    out = ebrowse(env, "expand", "s2", "--all").stdout
    assert re.search(r"\[Save changes \(@e\d+ \?\)\]", out), out
    assert not re.search(r"Settings saved automatically \(@e", out)
    save = ref_for(env, "s2", r"\[Save changes")
    r = ebrowse(env, "click", save)
    assert r.returncode == 0, r.stderr
    assert "Changes saved" in r.stdout
    # the aria-expanded flip on a candidate is a tracked state change, and the
    # revealed links appear with fresh refs
    toggle = ref_for(env, "s2", r"Notification preferences")
    r = ebrowse(env, "click", toggle)
    assert r.returncode == 0, r.stderr
    assert re.search(r"expanded: \"false\" → \"true\"", r.stdout)
    assert "Manage alerts" in r.stdout


def test_full_page_veil_exposed_and_keyboard_fallback(server, env):
    # a full-viewport childless clickable overlay must get a ref of its own
    # (splitter: oversized childless nodes are terminal). The covered target
    # is a native button and the veil is not a modal (no dialog/inert/trap),
    # so the click completes via the keyboard fallback; diagnose still names
    # the veil's ref as the pointer-route blocker
    ebrowse(env, "open", server.url("veil_overlay.html"))
    r = ebrowse(env, "outline")
    assert "value your privacy" in r.stdout, r.stdout
    sub = ref_for(env, "s2", r"\[Subscribe")
    r = ebrowse(env, "diagnose", sub)
    assert "actionability: BLOCKED" in r.stdout
    m = re.search(r"dismiss or interact with (@e\d+)", r.stdout)
    assert m, r.stdout
    r = ebrowse(env, "click", sub)
    assert r.returncode == 0, r.stderr
    assert "Subscribed!" in r.stdout
    assert "activated via keyboard" in r.stdout
    # the veil is still up (keyboard didn't click through it); its own ref works
    r = ebrowse(env, "click", m.group(1))
    assert r.returncode == 0, r.stderr
    assert "disappeared" in r.stdout or "removed" in r.stdout


def test_diagnose_label_decoration_and_modal_cover(server, env):
    # label decoration over a fancy control is PASS (label activation), and a
    # dialog-guarded target must NOT be keyboard-activated — it stays blocked
    ebrowse(env, "open", server.url("dialogs.html"))
    ebrowse(env, "click", "#modal-btn")
    r = ebrowse(env, "click", "#covered-btn")  # native button under modal backdrop
    assert r.returncode == 1
    assert "dialog is open" in r.stderr
    assert "activated via keyboard" not in r.stdout + r.stderr
    ebrowse(env, "click", "#accept-cookies")
    ebrowse(env, "open", server.url("styled_controls.html"))
    news = ref_for(env, "s2", r"Subscribe to the deals")
    r = ebrowse(env, "diagnose", news)
    assert r.returncode == 0, r.stderr
    assert "actionability: PASS" in r.stdout
    assert "label" in r.stdout


def test_fancy_radio_clicks_despite_decorative_cover(server, env):
    # Amazon-style restyled radio: transparent native input whose center is
    # covered by a decorative sibling <i> inside the wrapping label. The old
    # center-point preflight hard-blocked this; label semantics make it valid.
    ebrowse(env, "open", server.url("styled_controls.html"))
    eur = ref_for(env, "s2", r"EUR - Euro")
    r = ebrowse(env, "click", eur)
    assert r.returncode == 0, r.stderr
    assert "covered by" not in r.stderr
    assert re.search(r'checked: "false" → "true"', r.stdout)
    assert "note: clicked via the associated label" in r.stdout


def test_fancy_external_label_checkbox(server, env):
    # external <label for=...> owns the visual box that covers the input center
    news = ref_for(env, "s2", r"Subscribe to the deals")
    r = ebrowse(env, "click", news)
    assert r.returncode == 0, r.stderr
    assert re.search(r'checked: "false" → "true"', r.stdout)


def test_check_uncheck_via_label_on_fancy_checkbox(server, env):
    # check/uncheck on a restyled checkbox whose input center is covered by
    # label decoration: routed through the label with a verified postcondition
    ebrowse(env, "open", server.url("styled_controls.html"))
    news = ref_for(env, "s2", r"Subscribe to the deals")
    r = ebrowse(env, "check", news)
    assert r.returncode == 0, r.stderr
    assert re.search(r'checked: "false" → "true"', r.stdout)
    assert "note: checked via the associated label" in r.stdout
    # already-checked check is a clean no-op, not a toggle
    r = ebrowse(env, "check", news)
    assert r.returncode == 0, r.stderr
    assert "no change detected" in r.stdout
    r = ebrowse(env, "uncheck", news)
    assert r.returncode == 0, r.stderr
    assert re.search(r'checked: "true" → "false"', r.stdout)


def test_spa_mutation_and_noop(server, env):
    ebrowse(env, "open", server.url("spa.html"))
    inp = ref_for(env, "s2", r"New task title")
    add = ref_for(env, "s2", r"\[Add task")
    ebrowse(env, "fill", inp, "Buy beans")
    r = ebrowse(env, "click", add)
    assert r.returncode == 0, r.stderr
    assert "→ partial change" in r.stdout and "Buy beans" in r.stdout
    r = ebrowse(env, "click", "#noop-btn")
    assert r.returncode == 0
    assert "no change detected" in r.stdout


def test_spa_route_swap_shows_sections(server, env):
    stats = ref_for(env, "s1", r"\[Stats")
    r = ebrowse(env, "click", stats)
    assert r.returncode == 0, r.stderr
    assert "[appeared]" in r.stdout
    assert "disappeared" in r.stdout


def test_iframe_form_flow(server, env):
    ebrowse(env, "open", server.url("iframe.html"))
    card = ref_for(env, "s2", r"Card number")
    r = ebrowse(env, "fill", card, "4242 4242 4242 4242")
    assert r.returncode == 0, r.stderr
    pay = ref_for(env, "s2", r"\[Pay")
    r = ebrowse(env, "click", pay)
    assert r.returncode == 0, r.stderr
    assert "Payment accepted." in r.stdout


def test_iframe_without_id_form_flow(server, env):
    # an iframe with no id/title used to capture refs that locate() could
    # never resolve (frame identity fell back to the frame URL)
    ebrowse(env, "open", server.url("iframe_noid.html"))
    card = ref_for(env, "s2", r"Card number")
    r = ebrowse(env, "fill", card, "4242 4242 4242 4242")
    assert r.returncode == 0, r.stderr
    pay = ref_for(env, "s2", r"\[Pay")
    r = ebrowse(env, "click", pay)
    assert r.returncode == 0, r.stderr
    assert "Payment accepted." in r.stdout


def test_link_click_is_navigation_landing(server, env):
    ebrowse(env, "open", server.url("list.html"))
    ebrowse(env, "open", server.url("form.html"))
    link = ref_for(env, "s1", r"\[Products")
    r = ebrowse(env, "click", link)
    assert r.returncode == 0, r.stderr
    # a navigating action returns a landing line, not a full outline
    assert "→ navigation" in r.stdout
    assert "now at" in r.stdout and "list.html" in r.stdout
    assert "run 'ebrowse outline'" in r.stdout
    assert "PAGE" not in r.stdout
    # durable refs stay live post-navigation without an explicit outline
    assert "Espresso Gear" in ebrowse(env, "outline").stdout


def test_scroll_reports_position(server, env):
    ebrowse(env, "open", server.url("huge.html"))
    r = ebrowse(env, "scroll", "down", "--pages", "2")
    assert r.returncode == 0, r.stderr
    assert re.search(r"scroll position y=\d+", r.stdout)


def test_eval_returns_result(server, env):
    r = ebrowse(env, "eval", "1 + 41")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("result: 42")


def test_stale_ref_errors_cleanly(server, env):
    r = ebrowse(env, "click", "@e99999")
    assert r.returncode == 2
    assert "stale ref" in r.stderr or "outline" in r.stderr


def test_native_dialog_expands_and_blocks_outside(server, env):
    ebrowse(env, "open", server.url("native_modal.html"))
    # a native <dialog>.showModal() surfaces as a dialog section, expanded inline
    r = ebrowse(env, "click", "#open-native")
    assert r.returncode == 0, r.stderr
    assert "→ dialog" in r.stdout
    assert "## " in r.stdout and "Close" in r.stdout and "(@e" in r.stdout
    # the modal's backdrop covers the page → outside click blocked geometrically
    r = ebrowse(env, "click", "#outside")
    assert r.returncode == 1
    assert "blocked" in r.stderr and "dialog" in r.stderr.lower()


def test_inert_modal_coalesced_and_names_block(server, env):
    ebrowse(env, "open", server.url("inert_modal.html"))
    # an aria-modal div coalesced into the content section is still flagged dialog
    r = ebrowse(env, "click", "#open")
    assert r.returncode == 0, r.stderr
    assert "→ dialog" in r.stdout and "[dialog]" in r.stdout
    # it blocks the page via inert (no covering element) → Playwright can't click;
    # the error names the specific modal instead of "an overlay is probably open"
    r = ebrowse(env, "click", "#outside")
    assert r.returncode == 1
    assert "modal is open" in r.stderr and "Trap modal" in r.stderr
