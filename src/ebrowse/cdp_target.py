"""Act-time CDP node binding: the rescue path for refs whose descriptors
cannot be located on the live page (ADR 0015).

Capture (engine="cdp") records a backendNodeId per element. When
locate.resolve() refuses — an anonymous icon button, a descriptor whose every
candidate mismatches — the session falls back to a CdpTarget bound to the
exact node the outline described. A dead binding (navigation, node replaced)
resolves to None and the caller raises the descriptor error: refuse > misbind
is unchanged, the binding only ever points at the observed node.

CdpTarget duck-types the slice of the Playwright Locator/ElementHandle API
that actions.py and interaction.py drive, so every verb and the whole
pointer-planning pipeline (occlusion probes, keyboard fallback) work
unchanged. Page-side evaluates run in a private isolated world
(Page.createIsolatedWorld) — never the main world, where prototype traps
could observe or poison them. Pointer/keyboard input goes through
page.mouse/page.keyboard (the same trusted CDP Input events Playwright
dispatches for locators).

Main-frame nodes only: resolving into the isolated world fails for nodes in
child frames, in which case evaluates degrade to best-effort ({} probes) while
protocol ops (scroll/focus) and coordinate input still work.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import CDPSession, Page

from ebrowse import debug
from ebrowse.errors import CommandError, ExitCode

_WORLD_NAME = "__ebrowse_act"


class BindingGone(Exception):
    """The bound node is detached or the document navigated."""


class CdpBridge:
    """One lazy CDP session + isolated world per Session page."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self._cdp: CDPSession | None = None
        self._world: int | None = None

    async def cdp(self) -> CDPSession:
        if self._cdp is None:
            self._cdp = await self.page.context.new_cdp_session(self.page)
        return self._cdp

    def invalidate(self) -> None:
        self._cdp = None
        self._world = None

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await (await self.cdp()).send(method, params or {})  # type: ignore[arg-type]
        except Exception:
            # one reconnect: the session dies with tab switches/closures
            self.invalidate()
            return await (await self.cdp()).send(method, params or {})  # type: ignore[arg-type]

    async def world(self) -> int:
        """Execution context id of our isolated world in the main frame
        (recreated after navigation invalidates it)."""
        if self._world is None:
            tree = await self.send("Page.getFrameTree")
            frame_id = tree["frameTree"]["frame"]["id"]
            r = await self.send(
                "Page.createIsolatedWorld", {"frameId": frame_id, "worldName": _WORLD_NAME}
            )
            self._world = int(r["executionContextId"])
        return self._world

    async def alive(self, backend_id: int) -> dict[str, Any] | None:
        """DOM.describeNode: the node's live description, or None if gone."""
        try:
            r = await self.send("DOM.describeNode", {"backendNodeId": backend_id})
            return r.get("node")
        except Exception:
            return None


class CdpTarget:
    """A bound node impersonating the Locator/ElementHandle surface the verbs
    use. Construct via CdpTarget.create() — returns None when the binding is
    dead so callers keep their descriptor error."""

    def __init__(self, bridge: CdpBridge, backend_id: int, ref: str) -> None:
        self._b = bridge
        self._id = backend_id
        self.ref = ref

    @classmethod
    async def create(cls, bridge: CdpBridge, backend_id: int, ref: str,
                     expect_tag: str | None = None) -> CdpTarget | None:  # fmt: skip
        node = await bridge.alive(backend_id)
        if node is None:
            return None
        if expect_tag and node.get("nodeName", "").lower() != expect_tag:
            return None  # id reuse across documents — treat as dead
        return cls(bridge, backend_id, ref)

    # ------------------------------------------------------------ evaluate ----

    async def _object_id(self) -> str:
        world = await self._b.world()
        try:
            r = await self._b.send(
                "DOM.resolveNode", {"backendNodeId": self._id, "executionContextId": world}
            )
        except Exception as e:
            # the world dies on navigation; recreate once and retry
            self._b._world = None
            world = await self._b.world()
            try:
                r = await self._b.send(
                    "DOM.resolveNode", {"backendNodeId": self._id, "executionContextId": world}
                )
            except Exception:
                raise BindingGone(str(e)) from e
        oid = r.get("object", {}).get("objectId")
        if not oid:
            raise BindingGone("node did not resolve")
        return oid

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        """handle.evaluate parity: js is '(el) => ...' or '(el, arg) => ...'.
        Runs in OUR isolated world (stealth: main-world traps never see it)."""
        oid = await self._object_id()
        args = [] if arg is None else [{"value": json.loads(json.dumps(arg))}]
        r = await self._b.send(
            "Runtime.callFunctionOn",
            {
                "objectId": oid,
                "functionDeclaration": f"function(...a) {{ return ({js})(this, ...a); }}",
                "arguments": args,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if r.get("exceptionDetails"):
            desc = r["exceptionDetails"].get("exception", {}).get("description", "evaluate failed")
            raise RuntimeError(desc.splitlines()[0])
        return r.get("result", {}).get("value")

    async def evaluate_handle(self, js: str, arg: Any = None) -> Any:
        raise NotImplementedError("CdpTarget has no JS handles")  # label route degrades

    async def element_handle(self, timeout: float | None = None) -> CdpTarget:
        return self  # interaction probes call handle.evaluate — same surface

    async def owner_frame(self) -> None:
        return None  # main-frame semantics; _cover_above returns {}

    # ------------------------------------------------------------- geometry ----

    async def bounding_box(self, timeout: float | None = None) -> dict[str, float] | None:
        try:
            r = await self._b.send("DOM.getContentQuads", {"backendNodeId": self._id})
            quads = r.get("quads") or []
        except Exception:
            return None
        if not quads:
            return None
        xs = [v for q in quads for v in q[0::2]]
        ys = [v for q in quads for v in q[1::2]]
        return {"x": min(xs), "y": min(ys), "width": max(xs) - min(xs), "height": max(ys) - min(ys)}

    async def _center(self) -> tuple[float, float]:
        box = await self.bounding_box()
        if box is None or box["width"] <= 0 or box["height"] <= 0:
            raise CommandError(
                f"{self.ref}: the bound element has no visible box — it may be "
                "hidden now; run 'ebrowse outline'",
                ExitCode.ACTION_FAILED,
            )
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    async def scroll_into_view_if_needed(self, timeout: float | None = None) -> None:
        await self._b.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": self._id})

    # ---------------------------------------------------------------- state ----

    async def is_disabled(self, timeout: float | None = None) -> bool:
        return bool(
            await self.evaluate(
                """(el) => {
                    try { if (el.matches(':disabled')) return true; } catch (e) {}
                    return el.getAttribute('aria-disabled') === 'true';
                }"""
            )
        )

    async def is_checked(self, timeout: float | None = None) -> bool:
        v = await self.evaluate(
            "(el) => 'checked' in el ? !!el.checked : el.getAttribute('aria-checked') === 'true'"
        )
        return bool(v)

    async def get_attribute(self, name: str, timeout: float | None = None) -> str | None:
        return await self.evaluate("(el, n) => el.getAttribute(n)", name)

    # -------------------------------------------------------------- actions ----

    async def focus(self) -> None:
        await self._b.send("DOM.focus", {"backendNodeId": self._id})

    async def click(self, trial: bool = False, timeout: float | None = None,
                    button: str = "left", click_count: int = 1,
                    modifiers: list[str] | None = None, **_: Any) -> None:  # fmt: skip
        await self.scroll_into_view_if_needed()
        x, y = await self._center()
        if trial:
            # actionability approximation: the center point must hit the
            # element (or a descendant/label surface). A miss raises so
            # _plan_pointer takes its obstructed route, same as a locator.
            hit = await self.evaluate(
                """(el, pt) => {
                    const t = document.elementFromPoint(pt[0], pt[1]);
                    let n = t;
                    while (n) {
                        if (n === el) return true;
                        n = n.parentNode || (n instanceof ShadowRoot ? n.host : null);
                    }
                    return false;
                }""",
                [x, y],
            )
            if not hit:
                raise RuntimeError(f"trial click at ({x:.0f},{y:.0f}) does not reach {self.ref}")
            return
        keys = _resolve_modifiers(modifiers)
        for k in keys:
            await self._b.page.keyboard.down(k)
        try:
            await self._b.page.mouse.click(x, y, button=button, click_count=click_count)  # type: ignore[arg-type]
        finally:
            for k in reversed(keys):
                await self._b.page.keyboard.up(k)

    async def hover(self, timeout: float | None = None) -> None:
        await self.scroll_into_view_if_needed()
        x, y = await self._center()
        await self._b.page.mouse.move(x, y)

    async def fill(self, text: str, timeout: float | None = None) -> None:
        """Playwright-fill parity: focus, select existing content, then insert
        the text as trusted input events (Input.insertText via keyboard)."""
        await self.scroll_into_view_if_needed()
        await self.focus()
        await self.evaluate(
            """(el) => {
                if (typeof el.select === 'function') { el.select(); return; }
                if (el.isContentEditable) {
                    const r = document.createRange();
                    r.selectNodeContents(el);
                    const s = window.getSelection();
                    s.removeAllRanges(); s.addRange(r);
                }
            }"""
        )
        if text:
            await self._b.page.keyboard.insert_text(text)
        else:
            await self._b.page.keyboard.press("Delete")

    async def press_sequentially(self, text: str, timeout: float | None = None,
                                 delay: float | None = None) -> None:  # fmt: skip
        await self.focus()
        await self._b.page.keyboard.type(text, delay=delay)

    async def press(self, key: str, timeout: float | None = None) -> None:
        await self.focus()
        await self._b.page.keyboard.press(key)

    async def set_checked(self, checked: bool, timeout: float | None = None) -> None:
        if await self.is_checked() == checked:
            return
        await self.click()
        if await self.is_checked() != checked:
            raise CommandError(
                f"could not set {self.ref} to {'checked' if checked else 'unchecked'} — "
                "the click did not change its state; run 'ebrowse outline'",
                ExitCode.ACTION_FAILED,
            )

    async def select_option(self, label: list[str] | None = None,
                            value: list[str] | None = None,
                            timeout: float | None = None) -> list[str]:  # fmt: skip
        """Native-select parity with locator.select_option: match options,
        set selected, dispatch input+change (what Playwright itself does)."""
        selected = await self.evaluate(
            """(el, want) => {
                if (el.tagName.toLowerCase() !== 'select') return null;
                const byLabel = want.byLabel;
                const vals = [];
                for (const o of el.options) o.selected = false;
                for (const w of want.items) {
                    let hit = null;
                    for (const o of el.options) {
                        const key = byLabel ? o.label.trim() : o.value;
                        if (key === w) { hit = o; break; }
                    }
                    if (!hit) return null;
                    hit.selected = true;
                    vals.push(hit.value);
                    if (!el.multiple) break;
                }
                el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return vals;
            }""",
            {"byLabel": value is None, "items": list(label or value or [])},
        )
        if selected is None:
            raise RuntimeError(f"no option matched on {self.ref}")
        return selected

    async def set_input_files(self, files: Any, timeout: float | None = None) -> None:
        paths = [str(f) for f in (files if isinstance(files, (list, tuple)) else [files])]
        await self._b.send("DOM.setFileInputFiles", {"files": paths, "backendNodeId": self._id})

    async def drag_to(self, dst: Any, timeout: float | None = None) -> None:
        await self.scroll_into_view_if_needed()
        await manual_drag(self._b.page, self, dst)


async def manual_drag(page: Page, src: Any, dst: Any) -> None:
    """Pointer-sequence drag between any two endpoints exposing bounding_box()
    (Locator or CdpTarget) — used whenever either end is binding-rescued."""

    async def center(obj: Any, what: str) -> tuple[float, float]:
        box = await obj.bounding_box()
        if box is None:
            raise CommandError(
                f"drag {what} has no visible box — run 'ebrowse outline'",
                ExitCode.ACTION_FAILED,
            )
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    sx, sy = await center(src, "source")
    tx, ty = await center(dst, "target")
    await page.mouse.move(sx, sy)
    await page.mouse.down()
    await page.mouse.move(tx, ty, steps=12)
    await page.mouse.up()


def _resolve_modifiers(modifiers: list[str] | None) -> list[str]:
    keys = []
    for m in modifiers or []:
        keys.append(
            "Meta" if m == "ControlOrMeta" and _is_mac() else m.replace("ControlOrMeta", "Control")
        )
    return keys


def _is_mac() -> bool:
    import sys

    return sys.platform == "darwin"


async def rescue_target(bridge: CdpBridge | None, bindings: dict[str, int],
                        ref: str | None, tag: str) -> CdpTarget | None:  # fmt: skip
    """The act-time fast path: a live CdpTarget for a ref the descriptor chain
    refused, or None (binding absent/dead) so the caller raises the descriptor
    error unchanged."""
    if bridge is None or not ref:
        return None
    bid = bindings.get(ref)
    if bid is None:
        return None
    target = await CdpTarget.create(bridge, bid, ref, expect_tag=tag)
    debug.emit(
        "locate",
        "binding_rescue" if target is not None else "binding_dead",
        level="info" if target is not None else "warn",
        ref=ref,
        backend_id=bid,
    )
    return target
