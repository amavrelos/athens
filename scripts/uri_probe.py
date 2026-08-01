"""One-shot System.Uri characterization — measure, on the runtime pywebview
actually loads, the mechanism diagnosed behind 0.1.1's ERR_FILE_NOT_FOUND
(commit 61da7b8): a pythonnet-hosted CLR declares no target framework, which
puts .NET Framework's System.Uri in legacy V2 quirks, where the file: syntax
has no MayHaveQuery — '?' is swallowed into the PATH and escaped to %3F.

Run via .github/workflows/uri-probe.yml (workflow_dispatch), on Windows.

This script exits nonzero ONLY when the environment is not representative of
a user machine (non-netfx runtime, a declared target framework, a
PYTHONNET_RUNTIME override) — a "clean" measurement in such an environment
would be silently meaningless. The measured behaviour itself is REPORTED for
a human to read, never asserted, so it cannot become a job that fails the day
Microsoft changes .NET. The permanent regression test lives in
tests/test_shell.py (test_window_urls_survive_system_uri_round_trip) and
asserts the invariant, not the bug.
"""
import os
import sys

# the 0.1.1 URL shape (query) and the current fallback shape (fragment) —
# representative literals, not derived from the app, so the probe stays a
# pure System.Uri measurement with no athens imports to drag along
FILE_QUERY = ("file:///C:/Program%20Files/Athens/_internal/athens/ui/web/"
              "index.html?b=1754000000&ws=ws://127.0.0.1:8765")
FILE_FRAGMENT = ("file:///C:/Program%20Files/Athens/_internal/athens/ui/web/"
                 "index.html#b=1754000000&ws=ws://127.0.0.1:8765")
HTTP_QUERY = "http://127.0.0.1:8766/index.html?b=1754000000&ws=ws://127.0.0.1:8765"

_lines: list[str] = []


def say(text: str = "") -> None:
    print(text, flush=True)
    _lines.append(text)


def finish() -> None:
    """Mirror the report into the GitHub job summary for easy eyeballing."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("### System.Uri probe\n```\n" + "\n".join(_lines) + "\n```\n")


def bail(reason: str) -> None:
    say()
    say(f"NOT REPRESENTATIVE — {reason}.")
    say("A measurement here would say nothing about the runtime pywebview")
    say("loads on a user machine; fix the environment and re-run.")
    finish()
    sys.exit(1)


def describe(Uri, label: str, url: str):
    say(f"-- {label}")
    say(f"   input        : {url}")
    try:
        u = Uri(url)
    except Exception as exc:  # noqa: BLE001 — a .NET UriFormatException here
        # IS a finding: the URL shape is unusable on this runtime at all
        say(f"   Uri(...) THREW {type(exc).__name__}: {exc}")
        say("   -> CONFIRMS the mechanism (this URL shape cannot even be built)")
        say()
        return None
    say(f"   Query        : {str(u.Query)!r}")
    say(f"   AbsolutePath : {u.AbsolutePath}")
    say(f"   AbsoluteUri  : {u.AbsoluteUri}")
    say(f"   ToString()   : {u}")
    say()
    return u


def main() -> None:
    if not sys.platform.startswith("win"):
        bail("this probe must run on Windows")
    if os.environ.get("PYTHONNET_RUNTIME"):
        bail(f"PYTHONNET_RUNTIME={os.environ['PYTHONNET_RUNTIME']!r} is set — "
             "pywebview does a bare `import clr` (the DEFAULT runtime)")

    import clr  # noqa: F401 — bare import, exactly like webview/platforms/winforms.py
    from System import AppDomain, Uri
    from System.Runtime.InteropServices import RuntimeInformation

    desc = str(RuntimeInformation.FrameworkDescription)
    say(f"FrameworkDescription : {desc}")
    if not desc.startswith(".NET Framework"):
        bail("pythonnet resolved to a non-.NET-Framework runtime; on user "
             "machines the Windows default is netfx")

    try:
        tfm = AppDomain.CurrentDomain.SetupInformation.TargetFrameworkName
    except Exception as exc:  # noqa: BLE001
        tfm = f"<unreadable: {exc}>"
    say(f"TargetFrameworkName  : {tfm!r}")
    if tfm is not None:
        bail("a target framework is declared, so legacy V2 quirks would be "
             "OFF here — a pythonnet-hosted CLR on a user machine declares none")
    say()

    u = describe(Uri, "file:// + QUERY (the 0.1.1 URL shape)", FILE_QUERY)
    describe(Uri, "file:// + FRAGMENT (the current fallback shape)", FILE_FRAGMENT)
    describe(Uri, "http:// + QUERY (the current primary shape)", HTTP_QUERY)

    if u is None:
        pass  # verdict already printed by describe()
    elif str(u.Query) == "":
        say("VERDICT: CONFIRMED — the query was swallowed into the path "
            "(Query == ''), exactly the legacy-V2-quirks behaviour 61da7b8 "
            "names. The %3F in AbsoluteUri above is what Chromium was handed.")
    elif "%3F" in str(u.AbsoluteUri) or "%3F" in str(u):
        say("VERDICT: CONFIRMED (variant) — the query survives parsing but a "
            "serialization escapes '?' to %3F; whichever form the WinForms "
            "Source setter navigates with decides what Chromium sees.")
    else:
        say("VERDICT: NOT REPRODUCED — this runtime keeps the query intact. "
            "61da7b8's causal story is unconfirmed and should be revisited "
            "(the loopback-HTTP fix stands regardless, on MIME/origin/cache "
            "grounds).")
    finish()


if __name__ == "__main__":
    main()
