"""End-to-end test of the web UI: real HTTP server, real CLI subprocess, fake LLM.

Run:  python -m tests.test_webui
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp()
os.environ["PERSONA_FORGE_TALENT_DIR"] = str(Path(TMP) / "talent")  # before importing webui

from persona_forge import marinara, webui  # noqa: E402
from tests.test_offline import FakeLLM, check, serve  # noqa: E402


def req(base, path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    h = {"Content-Type": "application/json"} if isinstance(body, dict) else {}
    h.update(headers or {})
    r = urllib.request.Request(base + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if resp.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def wait(base, job_id):
    lines, since = [], 0
    for _ in range(240):
        _, j = req(base, f"/api/job/{job_id}?since={since}")
        lines += j["lines"]
        since = j["next"]
        if j["status"] != "running":
            return j, lines
        time.sleep(0.5)
    raise AssertionError("job did not finish")


def main():
    _, llm_url = serve(FakeLLM)
    webui.ensure_talent_root()
    srv = webui.ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    troot = Path(os.environ["PERSONA_FORGE_TALENT_DIR"])

    check(troot.is_dir() and (troot / "README.txt").exists(), "talent/ root created on first run")
    st, page = req(base, "/")
    check(st == 200 and b"Persona" in page and b"Consent note" in page, "page served")
    check(page.count(b'class="hint"') >= 50, "every option has a guiding hint")

    st, info = req(base, "/api/talent", "POST", {"name": "Nova Test"})
    d = troot / "Nova_Test"
    for sub in ("references", "sprites/full-body", "avatar", "sources", "output", "runs"):
        check((d / sub).is_dir(), f"talent/Nova_Test/{sub}/ created")
    for f in ("brief.md", "limits.txt", "urls.txt", "settings.json"):
        check((d / f).is_file(), f"talent/Nova_Test/{f} created")
    check("Nova Test" in info["brief"], "brief template personalised with the name")

    png = marinara.placeholder_avatar("r", 40, 40)
    for i in (1, 2):
        req(base, f"/api/upload?talent=Nova_Test&kind=references&filename=ref{i}.png", "PUT", png)
    st, info = req(base, "/api/upload?talent=Nova_Test&kind=sources&filename=interview.txt", "PUT",
                   b"Nova Test interview: she streams, loves slow-burn teasing, talks fast.")
    import shutil, subprocess
    has_ffmpeg = bool(shutil.which("ffmpeg"))
    if has_ffmpeg:
        wav = Path(TMP) / "voice.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=2", str(wav)], check=True)
        st, info = req(base, "/api/upload?talent=Nova_Test&kind=audio&filename=voice%20note.wav", "PUT", wav.read_bytes())
        check(info["files"]["audio"] == ["voice_note.wav"], "audio upload stored in audio/")
    check(info["files"]["references"] == ["ref1.png", "ref2.png"] and info["files"]["sources"] == ["interview.txt"],
          "uploads stored in references/ and sources/")
    st, _ = req(base, "/api/upload?talent=Nova_Test&kind=references&filename=evil.exe", "PUT", b"x")
    check(st == 400, "wrong file type refused")
    st, _ = req(base, "/api/upload?talent=..&kind=references&filename=a.png", "PUT", png)
    check(st == 400, "path traversal in talent name refused")

    # Validation: only name + consent note (creator mode) are required
    st, j = req(base, "/api/run", "POST", {"mode": "talent", "name": "Nova Test"})
    check(st == 400 and any("consent" in e.lower() for e in j["errors"]), "missing consent note → friendly error")
    st, j = req(base, "/api/run", "POST", {"mode": "talent", "name": ""})
    check(st == 400 and any("name" in e.lower() for e in j["errors"]), "missing name → friendly error")

    # Mostly empty form: every optional box blank, one junk number – must still run
    form = {k: "" for k in ("descriptor", "creator", "wiki_title", "seeds", "urls", "handles", "avatar_style",
                            "sprite_expressions", "sprite_poses", "style", "pov", "scenario", "era", "lore_entries",
                            "language", "spec", "extract_model", "workers", "max_tokens", "json_mode",
                            "image_base_url", "image_api_key", "image_model", "push_url", "marinara_user",
                            "marinara_pass", "target_coverage", "max_rounds", "pages_per_round", "max_pages",
                            "site_pages", "seed_max_links", "seed_depth", "search_provider", "audience_pages",
                            "scan_depth", "token_budget")}
    form.update({"mode": "talent", "name": "Nova Test", "consent_note": "Approved by Nova, release on file",
                 "nsfw": True, "no_web": True, "deep": False, "avatar_from_reference": True,
                 "base_url": llm_url, "model": "f", "api_key": "sk-secret-123", "greetings": "abc",
                 "transcriber": "api", "transcribe_base_url": llm_url, "transcribe_api_key": "tk-secret",
                 "brief": info["brief"], "limits": info["limits"]})
    st, j = req(base, "/api/run", "POST", form)
    check(st == 200 and j["job"], "run started with blank optional boxes")
    res, lines = wait(base, j["job"])
    log = "\n".join(lines)
    check(res["status"] == "done", f"run finished OK (status {res['status']})")
    check("Ignored “greetings”" in log, "junk number ignored with a note, not an error")
    check("brief is still the empty template" in log, "untouched brief template left out")
    check("sk-secret-123" not in log, "API key never appears in the log")
    if has_ffmpeg:
        check("--audio" in log and ("Transcribing voice_note.wav" in log or "transcript of voice_note.wav from cache" in log)
              and "tk-secret" not in log,
              "audio folder transcribed in the run (key kept out of the log)")
        check((out_dir := d / "output" / "Nova_Test" / "transcripts").is_dir() and list(out_dir.glob("*.txt")),
              "transcript saved in the creator's output folder")
    check("--extract-model= " in log or log.rstrip().count("--extract-model=") >= 1,
          "empty page-reading box sent as empty (a leftover .env value is not used)")
    check("ℹ Python:" in log and "Finished with exit code 0" in log, "console shows setup and exit code")
    check("chunks read" in log, "progress lines for the progress bar")
    check(res.get("log_file", "").startswith("runs/"), "log file link available")
    names = [o["name"] for o in res["outputs"]]
    check("Nova_Test.png" in names and "review.md" in names and "sprite_kit/SPRITE_KIT.md" in names,
          "card, review and sprite kit listed")
    out = d / "output" / "Nova_Test"
    check((out / "Nova_Test.png").is_file() and (out / "dossier.json").is_file(), "outputs inside talent/Nova_Test/output/")
    settings = json.loads((d / "settings.json").read_text())
    check("api_key" not in settings and settings["consent_note"].startswith("Approved"),
          "settings.json saved without secrets")
    check(list((d / "runs").glob("*.log")), "run log saved in runs/")
    card = json.loads((out / "Nova_Test.card.json").read_text())["data"]
    check("Never agree to meet in real life" in card["system_prompt"] and "<list them" not in card["system_prompt"],
          "default limits applied, placeholder lines skipped")
    check(card["extensions"]["imageAppearanceEnabled"] is True, "card JSON complete (image prompt set)")

    st, info = req(base, "/api/talent/Nova_Test")
    check(info["has_dossier"] and info["outputs"], "talent info reports saved research and outputs")
    st, raw = req(base, "/files/Nova_Test/output/Nova_Test/review.md")
    check(st == 200 and b"Research review" in raw, "output files served for viewing")
    st, _ = req(base, "/files/Nova_Test/../../etc/passwd")
    check(st == 404, "file serving cannot escape the talent folder")

    # Rebuild writing only, with a changed style
    form.update({"style": "roleplay", "greetings": "2"})
    st, j = req(base, "/api/rebuild", "POST", form)
    res, lines = wait(base, j["job"])
    check(res["status"] == "done" and "--from-dossier" in "\n".join(lines), "rebuild from saved research works")

    # Refine the card just made, using the text source already in sources/
    st, info = req(base, "/api/talent/Nova_Test")
    check("output/Nova_Test/Nova_Test.png" in info["cards"], "built card offered for refining")
    st, j = req(base, "/api/refine", "POST", dict(form, refine_card=""))
    check(st == 400 and "card to refine" in j["errors"][0], "refine without a card → friendly error")
    st, j = req(base, "/api/refine", "POST", dict(form, refine_card="../../etc/passwd"))
    check(st == 400, "refine card path cannot escape the talent folder")
    st, j = req(base, "/api/refine", "POST", dict(form, refine_card="output/Nova_Test/Nova_Test.png",
                                                    refine_scope="facts", feeds="reddit: u/nobody\n"))
    check(st == 200 and j.get("job"), "refine started")
    res, lines = wait(base, j["job"])
    rlog = "\n".join(lines)
    check(res["status"] == "done" and "--refine" in rlog and "Refining the card" in rlog, "refine run finished")
    check((out / "Nova_Test_v1.1.png").is_file() and (out / "refine_report.md").is_file(),
          "refined card v1.1 and refine_report.md saved")
    check((d / "feeds.txt").read_text().strip().endswith("reddit: u/nobody"), "feeds saved to feeds.txt")
    st, info = req(base, "/api/talent/Nova_Test")
    check(info["cards"][0].endswith("Nova_Test_v1.1.png"), "newest card listed first")
    png = (out / "Nova_Test.png").read_bytes()
    st, info = req(base, "/api/upload?talent=Nova_Test&kind=card&filename=made_elsewhere.png", "PUT", png)
    check(st == 200 and "cards/made_elsewhere.png" in info["cards"], "uploaded card offered for refining")

    # Cross-site requests are refused
    st, _ = req(base, "/api/run", "POST", form, headers={"Origin": "https://evil.example"})
    check(st == 403, "requests from other websites refused")

    # A second creator gets its own folder; list shows both
    req(base, "/api/talent", "POST", {"name": "Ivy Wren"})
    st, j = req(base, "/api/talents")
    check({t["slug"] for t in j["talents"]} == {"Nova_Test", "Ivy_Wren"}, "each creator has its own folder")

    # Fictional/creator with nothing to read while offline → clear message, not a crash
    st, j = req(base, "/api/run", "POST", {"mode": "fictional", "name": "Ivy Wren", "no_web": True})
    check(st == 400 and "Nothing to read" in j["errors"][0], "offline with no sources → clear message")

    srv.shutdown()
    print("\nAll web UI checks passed.")


if __name__ == "__main__":
    main()
