"""v1.0.1 Bug 2: the js_api object must not leak objects into pywebview's bridge.

pywebview's bridge generator (util.get_functions) recursively walks every
PUBLIC attribute of the js_api object when the page loads. A public attribute
holding the pywebview Window drags the walker through the entire native
(WinForms/WebView2 or Cocoa) object graph - thousands of interop getters under
the GIL, right after first paint - which starves the UI thread and produced
the Windows "not responding" hang in v1.0.0.
"""

import bitrebuttal.gui as gui


def test_window_api_exposes_only_methods():
    api = gui._WindowApi()
    public = [n for n in dir(api) if not n.startswith("_")]
    assert public, "expected the bridge methods (minimize/close/...) to be public"
    for name in public:
        assert callable(getattr(api, name)), (
            f"_WindowApi.{name} is a non-callable public attribute: pywebview's "
            "bridge generator recursively introspects it on page load (GIL storm "
            "-> UI thread freeze). Store state on underscore-prefixed attributes.")


def test_window_api_still_controls_its_window():
    """The rename must not break the actual window controls."""
    calls = []

    class W:
        def minimize(self):
            calls.append("min")

        def maximize(self):
            calls.append("max")

        def restore(self):
            calls.append("restore")

        def destroy(self):
            calls.append("destroy")

    api = gui._WindowApi()
    api._window = W()
    api.minimize()
    api.toggle_maximize()
    api.toggle_maximize()
    api.close()
    assert calls == ["min", "max", "restore", "destroy"]
