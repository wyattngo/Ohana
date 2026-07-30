"""Conformance tests cho ADP engine tools (spec 18 — DEC-OHANA-08: ohana = engine base).

Nguyên tắc: chạy tool bash THẬT qua subprocess, không mock. Fixture là git repo TẠM —
cô lập hoàn toàn khỏi repo ohana thật, nên test không phụ thuộc trạng thái
.gitignore/tracking hiện hành và không ghi gì vào sổ bằng chứng thật.

P0a — cặp test HAI CHIỀU cho exclusion trong `adp_work_diff_sha` (nghịch đảo KI-020):
  (a) ghi vào `docs/.adp-audit.jsonl` KHÔNG được đổi hash — audit log có 4 writer ngoài
      checkpoint (adp-review + 3 hook) bắn trong cửa sổ stamp→validate; nếu nó nằm trong
      hash thì track nó là mọi verdict hết bind (REFUSE oan — lý do .gitignore cũ tồn tại).
  (b) ghi vào file tracked KHÁC (red-proof làm nhân chứng) PHẢI đổi hash — chặn
      over-exclusion: nếu (b) đỏ nghĩa là exclusion nuốt quá tay và diff-binding đã chết
      mà không ai biết. Cặp (a)+(b) chỉ có nghĩa khi ĐI CÙNG NHAU.

Exclusion là danh sách LIỆT KÊ từng literal, không glob (Wyatt 2026-07-25): glob
`.adp-*` sẽ nuốt luôn `docs/.adp-state-hash` — thứ PHẢI nằm trong hash vì checkpoint
commit nó như evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# TH8: resolver adp_state_dir default về ${ADP_STATE_HOME:-$HOME}/.adp/<repo-id> —
# không scope thì mọi test chạy hook/lib trên fixture git sẽ rải store rác vào
# ~/.adp THẬT. Session-level tmp; test nào cần store riêng override qua env_extra.
os.environ.setdefault("ADP_STATE_HOME", tempfile.mkdtemp(prefix="adp-state-home-"))

ADP_LIB = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "adp-lib.sh"
ADP_REVIEW = Path(__file__).resolve().parent.parent / ".claude" / "tools" / "adp-review.sh"

# Tooling ADP (.claude/hooks + .claude/tools) CHƯA adopt sang repo OHANA — skip cả module
# thay vì đỏ 58 test về thứ không tồn tại. Điều kiện là sự tồn tại của file, không phải
# cờ env: ngày nào tooling sang là module tự sống lại, không cần ai nhớ gỡ skip.
pytestmark = pytest.mark.skipif(
    not ADP_LIB.exists(),
    reason="ADP tooling chưa adopt (.claude/hooks/adp-lib.sh không tồn tại) — tech-debt T6",
)


def _git(repo: Path, *args: str) -> None:
    # S603/S607 an toàn ở đây: argv literal + đường dẫn từ tmp_path fixture, không có
    # input ngoài; test CỐ Ý chạy binary thật (không mock) — đó là điểm của conformance.
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )


def _work_diff_sha(repo: Path) -> str:
    """Gọi thẳng hàm bash thật — đúng code đường stamp/checkpoint dùng, không mô phỏng."""
    out = subprocess.run(  # noqa: S603 — argv literal, path từ fixture (xem _git)
        ["bash", "-c", f'source "{ADP_LIB}" && adp_work_diff_sha "{repo}"'],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    sha = out.stdout.strip()
    assert len(sha) == 64, f"adp_work_diff_sha phải trả 64-hex, nhận: {sha!r}"
    return sha


@pytest.fixture()
def fake_repo(tmp_path: Path) -> Path:
    """Repo git tạm mô phỏng hình dạng ADP: audit log + red-proof + 1 file code, đều TRACKED.

    Cả audit log cũng tracked — đó chính là trạng thái P0b sẽ tạo ra trên repo thật,
    và là điều kiện làm test (a) đỏ được trước khi lib có exclusion.
    """
    repo = tmp_path / "fake"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / ".adp-audit.jsonl").write_text(
        '{"ts":"2026-07-25T00:00","gate":"review","outcome":"PASS"}\n'
    )
    (repo / "docs" / ".adp-red-proof.json").write_text('{"task-1":{"red_exit":1}}\n')
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed")
    return repo


def test_diff_hash_excludes_audit_log(fake_repo: Path) -> None:
    """(a) Append audit log → hash KHÔNG đổi. RED trước khi adp-lib có exclusion."""
    h0 = _work_diff_sha(fake_repo)
    with (fake_repo / "docs" / ".adp-audit.jsonl").open("a") as f:
        f.write('{"ts":"2026-07-25T00:01","gate":"review","outcome":"REFUSED"}\n')
    h1 = _work_diff_sha(fake_repo)
    assert h1 == h0, (
        "audit-log append đã đổi work-diff hash — mọi verdict stamp trước đó sẽ REFUSE oan "
        "(KI-020); adp_work_diff_sha phải exclude docs/.adp-audit.jsonl"
    )


def test_diff_hash_still_sees_other_tracked_files(fake_repo: Path) -> None:
    """(b) Chiều ngược — append red-proof (tracked, KHÔNG exclude) → hash PHẢI đổi.

    Red-proof cố ý nằm TRONG hash (phép thử thứ-tự 2026-07-25: 6 lần ghi in-phase khi còn
    tracked, 0 REFUSE oan — vì RED ghi trước stamp). Nếu test này đỏ: exclusion đã nuốt
    quá tay và diff-binding mù mà không ai hay.
    """
    h0 = _work_diff_sha(fake_repo)
    with (fake_repo / "docs" / ".adp-red-proof.json").open("a") as f:
        f.write('{"task-2":{"red_exit":1}}\n')
    h1 = _work_diff_sha(fake_repo)
    assert h1 != h0, (
        "red-proof append KHÔNG đổi hash — exclusion đang quá rộng (glob?), "
        "diff-binding chết im lặng"
    )


def test_diff_hash_sees_code_changes(fake_repo: Path) -> None:
    """(b') Nhân chứng thứ hai: sửa file code thường PHẢI đổi hash — sanity tối thiểu."""
    h0 = _work_diff_sha(fake_repo)
    (fake_repo / "app.py").write_text("x = 2\n")
    h1 = _work_diff_sha(fake_repo)
    assert h1 != h0, "code change không đổi hash — adp_work_diff_sha hỏng nền tảng"


def _stamp(
    repo: Path, verdict: dict[str, object], tag: str, reason: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Chạy `adp-review.sh stamp` THẬT trên repo tạm. Verdict src/out nằm NGOÀI repo
    (tmp_path cha) — không làm bẩn diff của fixture. `reason` → env ADP_RESTAMP_REASON."""
    src = repo.parent / f"verdict-{tag}.json"
    out = repo.parent / f"stamped-{tag}.json"
    src.write_text(json.dumps(verdict, ensure_ascii=False))
    env = dict(os.environ)
    env.pop("ADP_RESTAMP_REASON", None)  # cô lập khỏi env session — test tự khai reason
    if reason is not None:
        env["ADP_RESTAMP_REASON"] = reason
    return subprocess.run(  # noqa: S603 — argv literal, path từ fixture (xem _git)
        ["bash", str(ADP_REVIEW), "stamp", str(repo), str(src), str(out)],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
    )


def test_stamp_rejects_unknown_model(fake_repo: Path) -> None:
    """P1 (B3): stamp PHẢI từ chối verdict không khai model thật.

    `v.setdefault("model", "unknown")` là script BỊA field bắt buộc thay người gọi —
    16 bản ghi cũ mang model không kiểm được vì nó. Model giờ do người gọi khai thật:
    thiếu, hoặc ∈ {unknown, null, "", "?"} (kể cả JSON null) ⇒ exit ≠ 0, lỗi chỉ đúng
    field. Log cũ ở lại nguyên trạng (append-only) — fix chỉ chặn bản ghi MỚI.
    """
    base: dict[str, object] = {"verdict": "APPROVE"}
    bad_cases: list[dict[str, object]] = [
        base,  # thiếu model — case setdefault đang nuốt
        {**base, "model": "unknown"},
        {**base, "model": ""},
        {**base, "model": "null"},
        {**base, "model": "?"},
        {**base, "model": None},  # JSON null — setdefault không đụng, cũng phải chặn
    ]
    for i, verdict in enumerate(bad_cases):
        r = _stamp(fake_repo, verdict, f"bad{i}")
        assert r.returncode != 0, (
            f"stamp PHẢI reject verdict model rác {verdict.get('model', '<thiếu>')!r}, "
            f"nhưng exit 0 — stdout: {r.stdout!r}"
        )
        assert "model" in r.stderr, (
            f"thông báo lỗi phải chỉ đúng field 'model', nhận stderr: {r.stderr!r}"
        )

    good = _stamp(fake_repo, {**base, "model": "claude-haiku-4-5"}, "good")
    assert good.returncode == 0, (
        f"stamp phải NHẬN model thật, nhưng exit {good.returncode} — stderr: {good.stderr!r}"
    )
    stamped = (fake_repo.parent / "stamped-good.json").read_text()
    assert '"claude-haiku-4-5"' in stamped, (
        "model người gọi khai phải được giữ nguyên trong artifact"
    )


def test_stamp_records_restamp(fake_repo: Path) -> None:
    """P2 (B2): ghi đè artifact đã stamp cho diff KHÁC phải để lại dấu vết đếm được.

    "16 giây" không còn là một lần đọc lại diff im lặng: restamp cần `restamp_reason`
    (env ADP_RESTAMP_REASON) — thiếu ⇒ REFUSE; có ⇒ `restamp_count` tăng + reason ghi
    vào artifact. Ghi đè CÙNG diff (re-run vô hại) KHÔNG bị bắt reason — chỉ diff KHÁC
    mới là "verdict cũ gắn sang diff mới".
    """
    good: dict[str, object] = {"verdict": "APPROVE", "model": "claude-haiku-4-5"}
    art = fake_repo.parent / "stamped-rs.json"

    r0 = _stamp(fake_repo, good, "rs")
    assert r0.returncode == 0, f"stamp đầu phải pass — stderr: {r0.stderr!r}"
    a0 = json.loads(art.read_text())
    assert a0.get("restamp_count") == 0, (
        f"stamp đầu tiên phải ghi restamp_count=0, nhận: {a0.get('restamp_count')!r}"
    )

    r0b = _stamp(fake_repo, good, "rs")
    assert r0b.returncode == 0, (
        f"ghi đè CÙNG diff (re-run) không được đòi reason — stderr: {r0b.stderr!r}"
    )
    assert json.loads(art.read_text()).get("restamp_count") == 0, (
        "cùng diff mà count tăng — restamp accounting đếm nhầm re-run vô hại"
    )

    (fake_repo / "app.py").write_text("x = 99\n")  # diff đổi → verdict cũ hết áp dụng
    r1 = _stamp(fake_repo, good, "rs")
    assert r1.returncode != 0, (
        f"restamp diff KHÁC không reason phải REFUSE, nhưng exit 0 — stdout: {r1.stdout!r}"
    )
    assert "ADP_RESTAMP_REASON" in r1.stderr, (
        f"thông báo lỗi phải chỉ cách sửa (ADP_RESTAMP_REASON), nhận: {r1.stderr!r}"
    )

    r2 = _stamp(fake_repo, good, "rs", reason="diff đổi sau refactor, judge đã chấm lại")
    assert r2.returncode == 0, f"restamp CÓ reason phải pass — stderr: {r2.stderr!r}"
    a2 = json.loads(art.read_text())
    assert a2.get("restamp_count") == 1, (
        f"restamp phải tăng count 0→1, nhận: {a2.get('restamp_count')!r}"
    )
    assert a2.get("restamp_reason") == "diff đổi sau refactor, judge đã chấm lại", (
        f"restamp_reason phải ghi vào artifact, nhận: {a2.get('restamp_reason')!r}"
    )


def test_restamp_count_readable_by_checkpoint(tmp_path: Path) -> None:
    """`adp-checkpoint.sh` đọc restamp_count qua `adp_artifact_field` — helper phải trả
    được giá trị SỐ (json.dumps path), không chỉ str; nếu không, escalate chết im lặng."""
    art = tmp_path / "a.json"
    art.write_text('{"restamp_count": 2, "restamp_reason": "x"}')
    out = subprocess.run(  # noqa: S603 — argv literal, path từ tmp_path (xem _git)
        ["bash", "-c", f'source "{ADP_LIB}" && adp_artifact_field "{art}" restamp_count'],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "2", (
        f"adp_artifact_field phải đọc được int restamp_count, nhận: {out.stdout!r}"
    )


ADP_LINT = Path(__file__).resolve().parent.parent / ".claude" / "tools" / "adp-lint.sh"


def _lint(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — argv literal, path từ tmp_path (xem _git)
        ["bash", str(ADP_LINT), str(repo)],  # noqa: S607
        capture_output=True,
        text=True,
    )


def _memory(tmp_path: Path) -> Path:
    mem = tmp_path / "docs" / "memory"
    mem.mkdir(parents=True)
    return mem


def test_adp_lint_catches_conflict(tmp_path: Path) -> None:
    """P3 (X4): một ID mang dấu resolved ở file khai này + dấu open ở file khai kia
    ⇒ exit≠0 và ID xuất hiện trong output. Status/owner từng nằm trong câu văn không máy
    nào đọc — 13 mâu thuẫn sống song song nhiều tuần (đo §1.2 spec 18)."""
    mem = _memory(tmp_path)
    (mem / "KNOWN_ISSUES.md").write_text(
        "# KNOWN_ISSUES\n\n### ISSUE-999 — bug test\n- **Status:** RESOLVED (abc1234)\n"
    )
    (mem / "DECISIONS.md").write_text(
        "# DECISIONS\n\n### ISSUE-999 — bug test\n- **Status:** OPEN\n"
    )
    (mem / "SHIPPED-SURFACE.md").write_text("# SHIPPED\n(trống)\n")
    r = _lint(tmp_path)
    assert r.returncode != 0, (
        f"ISSUE-999 vừa RESOLVED vừa OPEN trong 2 file khai mà lint exit 0 — "
        f"stdout: {r.stdout!r} stderr: {r.stderr!r}"
    )
    assert "ISSUE-999" in (r.stdout + r.stderr), (
        f"output phải nêu đúng ID mâu thuẫn, nhận: {(r.stdout + r.stderr)!r}"
    )


def test_adp_lint_passes_clean(tmp_path: Path) -> None:
    """Fixture sạch (mỗi ID một trạng thái duy nhất) ⇒ exit 0."""
    mem = _memory(tmp_path)
    (mem / "KNOWN_ISSUES.md").write_text(
        "# KNOWN_ISSUES\n\n### ISSUE-998 — bug test\n- **Status:** RESOLVED (abc1234)\n"
        "\n### ISSUE-997 — bug khác\n- **Status:** OPEN\n"
    )
    (mem / "DECISIONS.md").write_text("# DECISIONS\n\n### DEC-001 — chọn X\n- **Status:** OPEN\n")
    (mem / "SHIPPED-SURFACE.md").write_text("# SHIPPED\n(trống)\n")
    r = _lint(tmp_path)
    assert r.returncode == 0, (
        f"fixture sạch mà lint fail — stdout: {r.stdout!r} stderr: {r.stderr!r}"
    )


def test_adp_lint_skips_narrative_files(tmp_path: Path) -> None:
    """File KỂ (SESSION_LOG, tasks/, adr/) KHÔNG bị quét — chống dương-tính-giả đã đo
    ở §1.2: 'phải đóng TRƯỚC' là lịch sử, không phải khai trạng thái sống."""
    mem = _memory(tmp_path)
    (mem / "KNOWN_ISSUES.md").write_text(
        "# KNOWN_ISSUES\n\n### ISSUE-996 — bug\n- **Status:** OPEN\n"
    )
    (mem / "DECISIONS.md").write_text("# DECISIONS\n")
    (mem / "SHIPPED-SURFACE.md").write_text("# SHIPPED\n")
    (mem / "SESSION_LOG.md").write_text(
        "# LOG\n\n### ISSUE-996 — đã bàn\n- **Status:** RESOLVED (nói mồm trong session)\n"
    )
    r = _lint(tmp_path)
    assert r.returncode == 0, (
        f"lint quét cả file kể (SESSION_LOG) — dương tính giả §1.2 quay lại: {r.stdout!r}"
    )


ADP_ENGINE = Path(__file__).resolve().parent.parent / ".claude" / "tools" / "adp-engine.sh"


def _engine(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — argv literal, path từ fixture (xem _git)
        ["bash", str(ADP_ENGINE), *args],  # noqa: S607
        capture_output=True,
        text=True,
    )


def test_engine_manifest_check_catches_drift(tmp_path: Path) -> None:
    """F0 spec 20: ranh giới engine↔instance thành code, đo HAI CHIỀU trên fixture.

    `list` khai tập file engine (test dựng fixture từ chính nó — không chép danh sách);
    `gen` stamp sha256 + version; `check` đỏ khi file ENGINE đổi/mất, và PHẢI xanh khi
    file INSTANCE (guardrail) đổi — over-coverage là cách install đè mất guardrail.
    `gen` từ chối stamp khi file engine tracked đang sửa dở (nửa chừng không có căn cước).
    """
    listing = _engine("list")
    assert listing.returncode == 0, f"list phải chạy được — stderr: {listing.stderr!r}"
    engine_files = [ln.strip() for ln in listing.stdout.splitlines() if ln.strip()]
    assert len(engine_files) >= 10, (
        f"tập engine bất thường ({len(engine_files)} file): {engine_files!r}"
    )
    assert not any("guardrail" in f for f in engine_files), (
        "guardrail.py là INSTANCE — nằm trong tập engine nghĩa là install sẽ đè nó"
    )

    repo = tmp_path / "target"
    for rel in engine_files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# fake engine file {rel}\n")
    (repo / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
    guardrail = repo / ".claude" / "hooks" / "guardrail.py"
    guardrail.write_text("# instance guardrail v1\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed")

    r_gen = _engine("gen", str(repo))
    assert r_gen.returncode == 0, f"gen trên fixture sạch phải pass — stderr: {r_gen.stderr!r}"
    manifest = repo / ".claude" / "engine.manifest"
    assert manifest.exists(), "gen phải ghi .claude/engine.manifest"
    assert "engine_version:" in manifest.read_text(), "manifest thiếu header engine_version"

    assert _engine("check", str(repo)).returncode == 0, "check ngay sau gen phải xanh"

    victim = repo / engine_files[0]
    original = victim.read_text()
    victim.write_text(original + "# drift\n")
    r_drift = _engine("check", str(repo))
    assert r_drift.returncode != 0, "file engine đổi mà check vẫn xanh — drift detector mù"
    assert engine_files[0] in (r_drift.stdout + r_drift.stderr), (
        f"check phải nêu đúng file lệch, nhận: {(r_drift.stdout + r_drift.stderr)!r}"
    )

    # gen từ chối stamp khi engine tracked đang sửa dở (victim chưa commit)
    r_dirty = _engine("gen", str(repo))
    assert r_dirty.returncode != 0, "gen trên tree có engine file sửa dở phải REFUSE"

    victim.write_text(original)  # hồi nguyên
    guardrail.write_text("# instance guardrail v2 — project tự sửa\n")
    r_inst = _engine("check", str(repo))
    assert r_inst.returncode == 0, (
        f"file INSTANCE đổi mà check đỏ — ranh giới nuốt quá tay: "
        f"{r_inst.stdout!r}{r_inst.stderr!r}"
    )


def test_engine_manifest_check_catches_deletion(tmp_path: Path) -> None:
    """File engine BIẾN MẤT cũng là drift — X1 spec 18 dạy: evidence untracked chết
    theo một lần đổi nhánh mà không ai hay."""
    listing = _engine("list")
    engine_files = [ln.strip() for ln in listing.stdout.splitlines() if ln.strip()]
    repo = tmp_path / "target"
    for rel in engine_files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# fake {rel}\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed")
    assert _engine("gen", str(repo)).returncode == 0
    (repo / engine_files[-1]).unlink()
    r = _engine("check", str(repo))
    assert r.returncode != 0, "xoá file engine mà check vẫn xanh"
    assert engine_files[-1] in (r.stdout + r.stderr), "check phải nêu file đã mất"


def test_adp_lint_declaring_from_manifest(tmp_path: Path) -> None:
    """F1 spec 20 (1/3): danh sách file khai đọc từ manifest key `LINT_DECLARING` —
    repo khác không bị ép dùng layout docs/memory của ohana. Khi khai, danh sách
    mặc định bị THAY (không phải cộng thêm): conflict trong file default phải vô hình.
    """
    repo = tmp_path / "target"
    (repo / "notes").mkdir(parents=True)
    (repo / "notes" / "STATE.md").write_text(
        "# STATE\n\n### ISSUE-995 — bug\n- **Status:** RESOLVED (abc)\n"
        "\n### ISSUE-995 — bug (chỗ khác)\n- **Status:** OPEN\n"
    )
    mem = repo / "docs" / "memory"
    mem.mkdir(parents=True)
    (mem / "KNOWN_ISSUES.md").write_text(
        "# KI\n\n### ISSUE-994 — bug default\n- **Status:** RESOLVED (x)\n"
        "\n### ISSUE-994 — bug default\n- **Status:** OPEN\n"
    )
    (repo / "CLAUDE.md").write_text(
        "# Target\n\n<!-- ADP:MANIFEST -->\nLINT_DECLARING: notes/STATE.md\n<!-- /ADP -->\n"
    )
    r = _lint(repo)
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"conflict trong file khai đã khai báo mà lint xanh: {out!r}"
    assert "ISSUE-995" in out, f"lint phải quét file từ LINT_DECLARING, nhận: {out!r}"
    assert "ISSUE-994" not in out, (
        f"LINT_DECLARING phải THAY danh sách mặc định, không cộng thêm — nhận: {out!r}"
    )


def test_adp_roadmap_title_from_basename(tmp_path: Path) -> None:
    """F1 spec 20 (2/3): header L3 lấy tên project từ basename repo — hết hardcode
    'ohana-ai'. Fixture tên khác → header mang tên đó."""
    repo = tmp_path / "zeta-proj"
    (repo / "docs" / "tasks").mkdir(parents=True)
    (repo / "docs" / "ROADMAP.md").write_text(
        "# ROADMAP\n\n## 4. Work items\n\n| ID | class |\n|---|---|\n| `GD0-ALPHA` | internal |\n"
    )
    _git(repo, "init", "-q")
    r = subprocess.run(  # noqa: S603 — argv literal, path từ fixture (xem _git)
        ["bash", str(ADP_LINT.parent / "adp-roadmap.sh"), str(repo)],  # noqa: S607
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"adp-roadmap trên fixture tối thiểu phải chạy: {r.stderr!r}"
    l3 = (repo / "docs" / "ROADMAP-STATUS.md").read_text()
    assert "ROADMAP STATUS — zeta-proj" in l3, (
        f"header L3 phải mang basename repo, nhận: {l3.splitlines()[0]!r}"
    )
    assert "ohana-ai" not in l3, "L3 của repo khác vẫn mang tên ohana-ai — hardcode chưa gỡ"


def test_adp_smoke_template_extracted(tmp_path: Path) -> None:
    """F1 spec 20 (3/3): checklist instance (`ohana-api`/`shop_id`/JWT) sống trong
    `.claude/smoke-templates.md` per-project — engine chỉ giữ khung generic. Repo
    không có template: khung vẫn chạy, KHÔNG rò chữ ohana; có template: emit nguyên văn.
    """
    smoke = ADP_LINT.parent / "adp-smoke.sh"
    repo = tmp_path / "target"
    (repo / "api").mkdir(parents=True)
    (repo / "api" / "foo.py").write_text("x = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed")
    (repo / "api" / "foo.py").write_text("x = 2\n")  # diff chạm api/ → section api bật

    def _new(tag: str) -> str:
        out = repo.parent / f"smoke-{tag}.md"
        r = subprocess.run(  # noqa: S603 — argv literal, path từ fixture (xem _git)
            ["bash", str(smoke), "new", str(repo), str(out)],  # noqa: S607
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, f"smoke new phải chạy được — stderr: {r.stderr!r}"
        return out.read_text()

    bare = _new("bare")
    assert "R2" in bare, "khung generic R2 phải còn khi không có template"
    for leak in ("ohana-api", "shop_id"):
        assert leak not in bare, (
            f"repo không có template mà checklist vẫn rò {leak!r} — instance chưa tách"
        )

    (repo / ".claude").mkdir()
    (repo / ".claude" / "smoke-templates.md").write_text(
        "<!-- SMOKE:server-cmd -->\n# preview_start acme-api → gọi endpoint → xem status\n"
        "<!-- /SMOKE -->\n<!-- SMOKE:api-extra -->\n"
        "### [ ] R4 — Rò tenant: gửi `tenant_id` giả trong body, grep log\n"
        "<!-- /SMOKE -->\n"
    )
    tpl = _new("tpl")
    assert "preview_start acme-api" in tpl, "template server-cmd không được emit"
    assert "Rò tenant" in tpl, "template api-extra không được emit"


def _fake_engine_source(tmp_path: Path, name: str = "src") -> Path:
    """Dựng SOURCE engine giả từ chính `adp-engine.sh list` (không chép danh sách),
    kèm templates instance — cô lập test install/drift khỏi repo ohana thật."""
    listing = _engine("list")
    engine_files = [ln.strip() for ln in listing.stdout.splitlines() if ln.strip()]
    src = tmp_path / name
    for rel in engine_files:
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# engine v1 {rel}\n")
    tpl = src / ".claude" / "templates"
    tpl.mkdir(parents=True)
    (tpl / "guardrail.py").write_text("# ĐIỀN: safety rules per-project\n")
    (tpl / "smoke-templates.md").write_text("<!-- ĐIỀN: checklist instance -->\n")
    _git(src, "init", "-q")
    _git(src, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(src, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed")
    r = subprocess.run(  # noqa: S603 — argv literal, path từ fixture (xem _git)
        ["bash", str(ADP_ENGINE), "gen", str(src)],  # noqa: S607
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"gen trên fake source phải pass: {r.stderr!r}"
    return src


def _engine_from(src: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["ADP_ENGINE_SOURCE"] = str(src)
    return subprocess.run(  # noqa: S603 — argv literal, path từ fixture (xem _git)
        ["bash", str(ADP_ENGINE), *args],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
    )


def test_engine_install_and_update_preserves_instance(tmp_path: Path) -> None:
    """F2 spec 20: install có đường về, và instance KHÔNG BAO GIỜ bị đè.

    Root cause 4 bản fork là install một chiều. Install: copy đúng tập manifest +
    stamp `engine.installed`; thiếu instance → chép template kèm marker ĐIỀN.
    Install LẠI = update: engine cũ bị đè, guardrail project tự sửa GIỮ NGUYÊN —
    chiều này sai là adopt xoá mất safety rules của một project thật.
    """
    src = _fake_engine_source(tmp_path)
    engine_files = [ln.strip() for ln in _engine("list").stdout.splitlines() if ln.strip()]
    target = tmp_path / "target"
    target.mkdir()

    r = _engine_from(src, "install", str(target))
    assert r.returncode == 0, f"install lần đầu phải pass — stderr: {r.stderr!r}"
    for rel in engine_files:
        assert (target / rel).exists(), f"install thiếu file engine {rel}"
    installed = target / ".claude" / "engine.installed"
    assert installed.exists(), "install phải stamp .claude/engine.installed"
    stamp = installed.read_text()
    assert "engine_version:" in stamp and "source_commit:" in stamp, (
        f"engine.installed thiếu version/commit: {stamp!r}"
    )
    guardrail = target / ".claude" / "hooks" / "guardrail.py"
    assert guardrail.exists() and "ĐIỀN" in guardrail.read_text(), (
        "target thiếu guardrail → phải được chép từ template kèm marker ĐIỀN"
    )

    guardrail.write_text("# project tự viết safety rules — KHÔNG được đè\n")
    victim = target / engine_files[0]
    victim.write_text("# target tự ý sửa engine\n")
    r2 = _engine_from(src, "install", str(target))
    assert r2.returncode == 0, f"install lại (update) phải pass: {r2.stderr!r}"
    assert victim.read_text() == f"# engine v1 {engine_files[0]}\n", (
        "update phải đè file engine về đúng bản source"
    )
    assert guardrail.read_text() == "# project tự viết safety rules — KHÔNG được đè\n", (
        "update ĐÈ guardrail — đúng thảm hoạ mà ranh giới instance tồn tại để chặn"
    )


def test_engine_install_drift_and_source_red_refusal(tmp_path: Path) -> None:
    """F2 spec 20: `drift` là đường về — target lệch source phải kêu, đúng file.
    Và cả install lẫn drift REFUSE khi SOURCE check đỏ: không phát tán engine
    đang drift (bản fork thứ 5 sinh ra đúng kiểu đó)."""
    src = _fake_engine_source(tmp_path)
    engine_files = [ln.strip() for ln in _engine("list").stdout.splitlines() if ln.strip()]
    target = tmp_path / "target"
    target.mkdir()
    assert _engine_from(src, "install", str(target)).returncode == 0
    assert _engine_from(src, "drift", str(target)).returncode == 0, (
        "target vừa install xong phải sạch drift"
    )

    (target / engine_files[1]).write_text("# lệch\n")
    r = _engine_from(src, "drift", str(target))
    assert r.returncode != 0, "target lệch mà drift exit 0 — đường về mù"
    assert engine_files[1] in (r.stdout + r.stderr), "drift phải nêu đúng file lệch"

    # source đỏ (engine file sửa dở chưa re-gen) → cả hai mode REFUSE
    (src / engine_files[2]).write_text("# source drifting\n")
    for mode in ("install", "drift"):
        rr = _engine_from(src, mode, str(target))
        assert rr.returncode != 0, (
            f"{mode} chạy được khi SOURCE check đỏ — đang phát tán engine drift"
        )


PRE_EDIT_GUARD = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "pre-edit-guard.sh"


def _guard_repo(tmp_path: Path) -> Path:
    """Fixture repo hình dạng Onfa tối giản: manifest RISK_PATHS substring money-words
    (bảng 21-A2 §S0 Wyatt APPROVED giữ nguyên), KHÔNG có phase active ⇒ đường warn."""
    repo = tmp_path / "target"
    (repo / "docs" / "tasks").mkdir(parents=True)
    (repo / "application" / "controllers").mkdir(parents=True)
    (repo / "application" / "views" / "wallet").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text(
        "# Target\n\n<!-- ADP:MANIFEST -->\nRISK_PATHS: cron, wallet\n"
        "SPEC_DIR: docs/tasks\n<!-- /ADP -->\n"
    )
    (repo / "application" / "controllers" / "Cron.php").write_text("<?php\n")
    (repo / "application" / "views" / "wallet" / "index.php").write_text("<?php\n")
    return repo


def _guard(repo: Path, rel: str) -> subprocess.CompletedProcess[str]:
    """Invoke pre-edit-guard THẬT qua hook-JSON contract (PreToolUse, stdin)."""
    payload = json.dumps(
        {"tool_input": {"file_path": str(repo / rel)}, "cwd": str(repo)},
        ensure_ascii=False,
    )
    return subprocess.run(  # noqa: S603 — argv literal, path từ fixture (xem _git)
        ["bash", str(PRE_EDIT_GUARD)],  # noqa: S607
        input=payload,
        capture_output=True,
        text=True,
    )


def test_pre_edit_guard_casefold_blocks_capcase(tmp_path: Path) -> None:
    """CF0 spec 22: token lowercase `cron` PHẢI cảnh báo file CapCase `Cron.php`.

    Đo 21-A2 bước 1: pre-edit-guard match case-SENSITIVE trong khi DoR + floor đã
    case-fold ⇒ money-file CapCase của Onfa (`Cron.php`, `Swap.php`, `Payout_cron.php`,
    `Balance_audit_library.php`…) NGOÀI guard — hôm nay, dưới cả v1.3 lẫn v2.3.
    RED trên guard hiện tại là toàn bộ nội dung phase CF0.
    """
    r = _guard(_guard_repo(tmp_path), "application/controllers/Cron.php")
    assert r.returncode == 0, f"đường warn phải exit 0 — stderr: {r.stderr!r}"
    assert "RISK" in r.stdout, (
        f"token 'cron' KHÔNG cảnh báo 'Cron.php' — lỗ CapCase còn sống. stdout: {r.stdout!r}"
    )


def test_pre_edit_guard_casefold_lowercase_already_warns(tmp_path: Path) -> None:
    """Chiều khoá hành vi cũ: path lowercase (`views/wallet/…`) đã cảnh báo TỪ TRƯỚC —
    fix case-fold không được làm mất nó (và test này phải xanh NGAY từ CF0)."""
    r = _guard(_guard_repo(tmp_path), "application/views/wallet/index.php")
    assert r.returncode == 0, f"đường warn phải exit 0 — stderr: {r.stderr!r}"
    assert "RISK" in r.stdout, f"hành vi lowercase-match có sẵn đã biến mất — regress: {r.stdout!r}"


ADP_DECIDE = Path(__file__).resolve().parent.parent / ".claude" / "tools" / "adp-decide.sh"


def _decision_artifact(path: Path, chosen: str | None) -> None:
    """Artifact adp-decision/v1 conformant qua adp_decision_validate: đúng 3 options
    id unique, đúng 1 recommended, title/approach non-empty, risk/effort hợp lệ."""
    d = {
        "schema": "adp-decision/v1",
        "id": "DEC-T-TH6",
        "trigger": "fixture",
        "context": "fixture TH6 — idempotency guard cho resolve",
        "options": [
            {
                "id": "A",
                "title": "opt A",
                "approach": "làm A",
                "risk": "low",
                "effort": "S",
                "recommended": True,
            },
            {"id": "B", "title": "opt B", "approach": "làm B", "risk": "low", "effort": "S"},
            {"id": "C", "title": "opt C", "approach": "làm C", "risk": "low", "effort": "S"},
        ],
        "chosen": chosen,
        "decided_by": "Original" if chosen else None,
        "ts": "2026-07-28T09:00:00+07:00" if chosen else None,
    }
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n")


def _decide_resolve(f: Path, choice: str, by: str) -> subprocess.CompletedProcess[str]:
    """Chạy `adp-decide.sh resolve` THẬT qua subprocess (không cần git repo —
    resolve chỉ đụng artifact + jsonl cạnh nó)."""
    return subprocess.run(  # noqa: S603 — argv literal, path từ fixture (xem _git)
        ["bash", str(ADP_DECIDE), "resolve", str(f), choice, by],  # noqa: S607
        capture_output=True,
        text=True,
    )


def test_decide_resolve_refuses_already_resolved(tmp_path: Path) -> None:
    """TH6 spec 23: resolve trên artifact đã có `chosen` non-null PHẢI REFUSE.

    Incident thật 2026-07-28 (DEC-OHANA-10): 2 resolve cách 50s — lần 2 ghi đè
    ts/decided_by và append dòng TRÙNG vào .adp-decisions.jsonl (append-only log),
    calibration counter của decision-gate đếm double, phải dọn tay. RED trên
    adp-decide.sh hiện tại là toàn bộ nội dung phase TH6.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    f = docs / "decision.json"
    _decision_artifact(f, chosen="A")
    before = f.read_text()
    log = docs / ".adp-decisions.jsonl"
    log.write_text('{"id": "DEC-T-TH6", "chosen": "A", "decided_by": "Original"}\n')

    r = _decide_resolve(f, "B", "SecondCaller")
    assert r.returncode == 1, (
        f"resolve lần 2 phải rc=1, nhận rc={r.returncode} — stdout: {r.stdout!r}"
    )
    assert "already resolved" in r.stderr, (
        f"stderr phải nêu 'already resolved' (kèm chosen/by/ts hiện tại): {r.stderr!r}"
    )
    assert f.read_text() == before, (
        "artifact đã resolved bị GHI ĐÈ (ts/decided_by mất dấu người quyết gốc)"
    )
    assert log.read_text().count("\n") == 1, (
        "log append-only bị double-append — calibration counter sẽ đếm double"
    )


def test_decide_resolve_open_artifact_still_works(tmp_path: Path) -> None:
    """Chiều khoá hành vi cũ: resolve lần đầu (chosen null) vẫn đi trọn đường —
    rc=0, set chosen/decided_by, append đúng 1 dòng log (không FAIL oan)."""
    docs = tmp_path / "docs"
    docs.mkdir()
    f = docs / "decision.json"
    _decision_artifact(f, chosen=None)

    r = _decide_resolve(f, "A", "Wyatt")
    assert r.returncode == 0, f"resolve lần đầu phải rc=0 — stderr: {r.stderr!r}"
    d = json.loads(f.read_text())
    assert d["chosen"] == "A" and d["decided_by"] == "Wyatt", f"artifact sau resolve: {d!r}"
    log = docs / ".adp-decisions.jsonl"
    assert log.is_file() and log.read_text().count("\n") == 1, (
        "resolve lần đầu phải append ĐÚNG 1 dòng log"
    )


# ---------------------------------------------------------------------------
# TH0 spec 23 — decision-gate.sh ghi audit đúng schema k=v (F4 audit chain 2026-07-28)
# ---------------------------------------------------------------------------

DECISION_GATE = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "decision-gate.sh"


def test_decision_gate_audit_schema(tmp_path: Path) -> None:
    """TH0 spec 23 (F4): audit line của decision-gate phải parse được theo schema k=v.

    Call site hiện tại truyền 1 arg JSON-string vào `adp_audit_event` (nhận k=v...)
    → dòng audit sinh key rác `{"decision-gate":"","{\"decision\"...}":""}` — mọi
    consumer filter theo `gate` (shadow-report, dashboard) không bao giờ thấy decision
    events. Verify thật 09:37 2026-07-28. RED trên decision-gate.sh hiện tại.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    _decision_artifact(docs / ".adp-decision-pending.json", chosen=None)

    env = dict(os.environ)
    env.pop("ADP_DECISION_ACTIVE", None)  # SHADOW: hook phải continue + vẫn ghi audit
    r = subprocess.run(  # noqa: S603 — argv literal, hook thật trên fixture tmp_path
        ["bash", str(DECISION_GATE)],  # noqa: S607
        input=json.dumps({"cwd": str(tmp_path)}),
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, f"SHADOW phải rc=0 — stderr: {r.stderr!r}"

    audit = docs / ".adp-audit.jsonl"
    assert audit.is_file(), "decision-gate phải ghi audit event khi có decision OPEN"
    obj = json.loads(audit.read_text().strip().splitlines()[-1])
    assert obj.get("gate") == "decision-gate", (
        f"audit line thiếu key gate='decision-gate' (schema k=v) — line thật: {obj!r}"
    )
    assert obj.get("decision") == "would-block", f"thiếu decision=would-block: {obj!r}"
    assert obj.get("id") == "DEC-T-TH6", f"thiếu id của decision đang open: {obj!r}"


# ---------------------------------------------------------------------------
# TH1 spec 23 — gate-verdict.sh: agent-mismatch là điều kiện FAIL (F3)
# ---------------------------------------------------------------------------

GATE_VERDICT = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "gate-verdict.sh"


def _cmd_sha(cmd: str) -> str:
    """adp_cmd_sha thật từ lib — đúng hàm gate-verdict dùng so acc_sha của RED-proof."""
    out = subprocess.run(  # noqa: S603 — argv literal (xem _git)
        ["bash", "-c", f'source "{ADP_LIB}" && adp_cmd_sha "{cmd}"'],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _gate_fixture(tmp_path: Path) -> Path:
    """Repo fixture PASS-baseline cho gate-verdict: mọi điều kiện FAIL khác đều xanh
    (acc rc=0, diff trong scope, RED-proof khớp acc_sha) — để CHỈ agent-mismatch
    quyết định verdict trong cặp test TH1."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed")
    (repo / "app.py").write_text("x = 2\n")  # diff thật, nằm trong files khai báo

    acc = "exit 0"
    # TH8: authority state sống ở trust store — fixture ghi đúng chỗ hook sẽ đọc,
    # resolve qua chính lib (không ghép tay path, bài học adp_risk.py).
    state = Path(_lib_call(f'adp_state_dir "{repo}"'))
    state.mkdir(parents=True, exist_ok=True)
    (state / ".adp-exec-state.json").write_text(
        json.dumps(
            {
                "schema": "adp-handoff/v1",
                "task_id": "t-th1",
                "current_agent": "coder",
                "acceptance_cmd": acc,
                "risk_tier": "low",
                "parent_phase": "TH1",
                "files": ["app.py"],
            }
        )
        + "\n"
    )
    (state / ".adp-red-proof.json").write_text(
        json.dumps({"t-th1": {"red_exit": 1, "acc_sha": _cmd_sha(acc)}}) + "\n"
    )
    return repo


def _run_gate_verdict(repo: Path, agent_type: str) -> dict[str, object]:
    """Chạy gate-verdict.sh THẬT với payload SubagentStop, trả artifact verdict."""
    env = dict(os.environ)
    env.pop("ADP_FORCE_SHADOW", None)  # cô lập khỏi env session
    subprocess.run(  # noqa: S603 — argv literal, hook thật trên fixture (xem _git)
        ["bash", str(GATE_VERDICT)],  # noqa: S607
        input=json.dumps({"cwd": str(repo), "agent_type": agent_type}),
        capture_output=True,
        text=True,
        env=env,
    )
    # TH8: snapshot sống ở trust store — resolve path qua chính lib (không ghép tay)
    artifact = Path(_lib_call(f'adp_gate_verdict_path "{repo}"'))
    assert artifact.is_file(), "gate-verdict phải ghi artifact snapshot"
    return json.loads(artifact.read_text())


def test_agent_mismatch_fails_verdict(tmp_path: Path) -> None:
    """TH1 spec 23 (F3): agent lệch exec-state PHẢI là FAIL, không phải flag suông.

    Verify 2026-07-28: spawn `bug-fixer` khi state ghi `coder` → artifact ra
    `verdict=PASS, flags=agent-mismatch:...` — verdict bind sai agent mà vẫn PASS,
    vi phạm yêu cầu định danh (NIST NCCoE 2/2026, accountability trong delegation
    chain). RED trên gate-verdict.sh hiện tại.
    """
    repo = _gate_fixture(tmp_path)
    obj = _run_gate_verdict(repo, agent_type="bug-fixer")
    assert "agent-mismatch:bug-fixer!=coder" in str(obj.get("flags", "")), (
        f"flag mismatch phải còn nguyên (định danh ghi vết): {obj!r}"
    )
    assert obj.get("verdict") == "FAIL", (
        f"agent-mismatch phải flip verdict FAIL — artifact thật: {obj!r}"
    )


def test_agent_match_keeps_pass_verdict(tmp_path: Path) -> None:
    """Chiều khoá TH1: agent khớp → verdict PASS theo acceptance, KHÔNG FAIL oan.

    Baseline fixture xanh mọi điều kiện khác — nếu test này đỏ sau fix nghĩa là
    nhánh mismatch FAIL cả trường hợp khớp (sai một ly FAIL oan mọi micro-task).
    """
    repo = _gate_fixture(tmp_path)
    obj = _run_gate_verdict(repo, agent_type="coder")
    assert obj.get("verdict") == "PASS", (
        f"agent khớp + mọi gate xanh phải PASS — artifact thật: {obj!r}"
    )
    assert "agent-mismatch" not in str(obj.get("flags", "")), (
        f"không được flag mismatch khi agent khớp: {obj!r}"
    )


# ---------------------------------------------------------------------------
# TH3 spec 23 — ADP_STRICT: fail-closed opt-in cho mắt xích thiếu exec-state (F2)
# ---------------------------------------------------------------------------

PROGRESS_GUARD = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "progress-guard.sh"


def _strict_repo(tmp_path: Path, *, strict: bool) -> Path:
    """Repo git tối thiểu, KHÔNG exec-state. strict=True ⇒ manifest khai ADP_STRICT: 1."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "f.txt").write_text("x\n")
    manifest = "ADP_STRICT: 1\n" if strict else ""
    (repo / "CLAUDE.md").write_text(
        f"# t\n\n<!-- ADP:MANIFEST -->\n{manifest}SPEC_DIR: docs/tasks\n<!-- /ADP -->\n"
    )
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed")
    return repo


def _run_hook(hook: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("ADP_FORCE_SHADOW", None)  # cô lập khỏi env session
    env.pop("ADP_STRICT", None)  # strict phải đến từ MANIFEST trong các test này
    return subprocess.run(  # noqa: S603 — argv literal, hook thật trên fixture (xem _git)
        ["bash", str(hook)],  # noqa: S607
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )


def test_adp_strict_progress_guard_blocks_without_handoff(tmp_path: Path) -> None:
    """TH3 (F2): ADP_STRICT bật + thiếu exec-state ⇒ spawn bị BLOCK rc=2, không im lặng.

    Audit chain 2026-07-28: MAIN bỏ pre-spawn ritual = zero gating — mắt xích LLM
    tự nguyện ở đầu trust chain. RED trên progress-guard hiện tại (shadow_out rc=0).
    """
    repo = _strict_repo(tmp_path, strict=True)
    r = _run_hook(PROGRESS_GUARD, {"cwd": str(repo), "tool_input": {"subagent_type": "coder"}})
    assert r.returncode == 2, (
        f"strict + no exec-state phải rc=2 — rc={r.returncode} out={r.stdout!r}"
    )
    assert "exec-state" in r.stderr, f"message block phải nêu nguyên nhân: {r.stderr!r}"
    assert "ADP_FORCE_SHADOW" in r.stderr, (
        f"block phải kèm đường thoát (escape hatch) — block không lối thoát là DoS: {r.stderr!r}"
    )


def test_adp_strict_gate_verdict_warns_without_state(tmp_path: Path) -> None:
    """TH3 (F2): gate-verdict thiếu exec-state dưới strict ⇒ CẢNH BÁO thay vì im lặng.

    Không block (rc vẫn 0): SubagentStop chặn việc ĐÃ chạy xong là vô nghĩa —
    điểm chặn là progress-guard (PreToolUse). Nhưng im lặng là F2: main phải THẤY.
    """
    repo = _strict_repo(tmp_path, strict=True)
    r = _run_hook(GATE_VERDICT, {"cwd": str(repo), "agent_type": "coder"})
    assert r.returncode == 0, f"gate-verdict strict vẫn continue — rc={r.returncode}"
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out.get("continue") is True
    assert "exec-state" in str(out.get("additionalContext", "")), (
        f"strict phải cảnh báo qua additionalContext, không im lặng: {r.stdout!r}"
    )


def test_adp_strict_default_off_progress_guard_unchanged(tmp_path: Path) -> None:
    """Chiều khoá: KHÔNG khai key ⇒ hành vi shadow_out giữ nguyên (rc=0, continue)."""
    repo = _strict_repo(tmp_path, strict=False)
    r = _run_hook(PROGRESS_GUARD, {"cwd": str(repo), "tool_input": {"subagent_type": "coder"}})
    assert r.returncode == 0, f"default phải shadow rc=0 — rc={r.returncode} err={r.stderr!r}"
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out.get("continue") is True, f"default phải continue: {r.stdout!r}"


# ---------------------------------------------------------------------------
# TH5 spec 23 — adp_state_dir: single resolver vị trí authority-state (ADR trust-store p.1)
# ---------------------------------------------------------------------------


def _lib_call(cmd: str, env_extra: dict[str, str] | None = None) -> str:
    """Gọi hàm bash thật từ adp-lib qua subprocess — đúng code hooks dùng."""
    env = dict(os.environ)
    env.pop("ADP_STATE_DIR", None)
    if env_extra:
        env.update(env_extra)
    out = subprocess.run(  # noqa: S603 — argv literal, hàm lib thật (xem _git)
        ["bash", "-c", f'source "{ADP_LIB}" && {cmd}'],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return out.stdout.strip()


def test_adp_state_dir_default_is_docs(tmp_path: Path) -> None:
    """TH5→TH8: fixture PHI-git không có repo-id ⇒ fallback docs/ (default git-repo
    giờ là trust store — xem test_state_authority_default_git_repo_is_trust_store)."""
    assert _lib_call(f'adp_state_dir "{tmp_path}"') == f"{tmp_path}/docs"


def test_adp_state_dir_env_override(tmp_path: Path) -> None:
    """TH5: ADP_STATE_DIR override — dành riêng cho suite parameterization."""
    override = str(tmp_path / "elsewhere")
    got = _lib_call(f'adp_state_dir "{tmp_path}"', {"ADP_STATE_DIR": override})
    assert got == override


def test_adp_exec_state_path_routes_through_resolver(tmp_path: Path) -> None:
    """TH5: helper path authority PHẢI route qua resolver (một nơi tính path duy nhất)."""
    override = str(tmp_path / "store")
    got = _lib_call(f'adp_exec_state_path "{tmp_path}"', {"ADP_STATE_DIR": override})
    assert got == f"{override}/.adp-exec-state.json", (
        f"exec_state_path không route qua adp_state_dir: {got!r}"
    )


def test_adp_repo_id_shared_across_worktrees(tmp_path: Path) -> None:
    """TH5: repo-id theo git-common-dir — worktree KHÔNG thoát được breaker
    (bài học competent-rhodes). Linked worktree phải cho CÙNG id với main."""
    main = tmp_path / "main"
    main.mkdir()
    (main / "f.txt").write_text("x\n")
    _git(main, "init", "-q")
    _git(main, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(main, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed")
    wt = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", str(wt))

    id_main = _lib_call(f'adp_repo_id "{main}"')
    id_wt = _lib_call(f'adp_repo_id "{wt}"')
    assert len(id_main) == 64, f"repo-id phải là sha256 64-hex: {id_main!r}"
    assert id_main == id_wt, "linked worktree phải chung repo-id (git-common-dir)"

    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    assert _lib_call(f'adp_repo_id "{other}"') != id_main, "repo khác phải khác id"


# ---------------------------------------------------------------------------
# TH7 spec 23 — pre-bash-guard sống lại + gác trust store (ADR trust-store p.0)
# ---------------------------------------------------------------------------

PRE_BASH_GUARD = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "pre-bash-guard.sh"
SETTINGS_JSON = Path(__file__).resolve().parent.parent / ".claude" / "settings.json"


def _run_bash_guard(command: str, cwd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — argv literal, hook thật (xem _git)
        ["bash", str(PRE_BASH_GUARD)],  # noqa: S607
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}),
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )


def test_bash_guard_registered_in_settings() -> None:
    """TH7 (ADR p.0): pre-bash-guard PHẢI được wire — engine_verify 2026-07-28 đếm nó
    trong 10 hook chết. Không wire thì mọi rule của nó (kể cả trust-store guard) là
    code chết, và `cat ~/.adp/key` không có gì cản. RED trên settings hiện tại."""
    hooks = json.loads(SETTINGS_JSON.read_text())["hooks"]
    bash_entries = [
        h["command"]
        for e in hooks.get("PreToolUse", [])
        if e.get("matcher") == "Bash"
        for h in e.get("hooks", [])
    ]
    assert any("pre-bash-guard.sh" in c for c in bash_entries), (
        f"PreToolUse matcher Bash phải trỏ pre-bash-guard.sh — hiện: {bash_entries!r}"
    )


def test_bash_guard_blocks_trust_store_access(tmp_path: Path) -> None:
    """TH7: lệnh Bash chạm ~/.adp trực tiếp → rc=2 BLOCK, message nêu lý do + đường
    thoát (entrypoint adp-*.sh). Không có rule này, key HMAC (TH9) chết bằng 1 lệnh cat."""
    for cmd in ("cat ~/.adp/key", 'cp "$HOME/.adp/key" /tmp/x', "rm -rf ~/.adp"):
        r = _run_bash_guard(cmd, str(tmp_path))
        assert r.returncode == 2, f"{cmd!r} phải bị block rc=2 — rc={r.returncode}"
        assert ".adp" in r.stderr, f"message phải nêu lý do trust-store: {r.stderr!r}"
        assert "adp-" in r.stderr and "tools" in r.stderr, (
            f"message phải kèm đường thoát (entrypoint .claude/tools/adp-*.sh): {r.stderr!r}"
        )


def test_bash_guard_allows_engine_entrypoints(tmp_path: Path) -> None:
    """Chiều khoá TH7: entrypoint engine hợp lệ KHÔNG bị chặn — checkpoint/status/
    review vẫn chạy qua Bash tool bình thường."""
    for cmd in (
        "bash .claude/tools/adp-status.sh",
        'bash .claude/tools/adp-checkpoint.sh -m "x"',
    ):
        r = _run_bash_guard(cmd, str(tmp_path))
        assert r.returncode == 0, f"{cmd!r} phải được allow — stderr: {r.stderr!r}"


def test_bash_guard_allows_normal_commands(tmp_path: Path) -> None:
    """Chiều khoá TH7: lệnh thường không liên quan store không bị đụng."""
    r = _run_bash_guard("ls -la && git status", str(tmp_path))
    assert r.returncode == 0, f"lệnh thường phải allow — stderr: {r.stderr!r}"


def test_adp_git_atleast_comparator() -> None:
    """TH5: check git ≥ 2.31 cho --path-format (adp-engine install trên máy khác).
    So sánh SỐ, không phải chuỗi: 2.9 < 2.31 (string-compare sẽ sai chiều)."""
    ok = _lib_call("adp_git_atleast 2.31 && echo yes || echo no", {"ADP_GIT_VERSION": "2.51.2"})
    assert ok == "yes"
    old = _lib_call("adp_git_atleast 2.31 && echo yes || echo no", {"ADP_GIT_VERSION": "2.30.1"})
    assert old == "no"
    tricky = _lib_call("adp_git_atleast 2.31 && echo yes || echo no", {"ADP_GIT_VERSION": "2.9.0"})
    assert tricky == "no", "2.9 phải < 2.31 — so sánh numeric từng segment, không so chuỗi"


def test_adp_strict_default_off_gate_verdict_unchanged(tmp_path: Path) -> None:
    """Chiều khoá: gate-verdict không strict ⇒ passthrough im lặng như cũ."""
    repo = _strict_repo(tmp_path, strict=False)
    r = _run_hook(GATE_VERDICT, {"cwd": str(repo), "agent_type": "coder"})
    assert r.returncode == 0
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out.get("continue") is True and not out.get("additionalContext"), (
        f"default phải passthrough không cảnh báo: {r.stdout!r}"
    )


# ---------------------------------------------------------------------------
# TH8 spec 23 — state authority dời về trust store ~/.adp/<repo-id>/ + hash-chain
# audit (ADR trust-store p.2, DEC-OHANA-10 option A). RED-first: cả cụm này viết
# TRƯỚC khi flip resolver — xem docs/.adp-red-proof.json task 23-TH8.
# ---------------------------------------------------------------------------


def _lib_rc(cmd: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Như _lib_call nhưng KHÔNG check — cho case đo rc và stderr."""
    env = dict(os.environ)
    env.pop("ADP_STATE_DIR", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(  # noqa: S603 — argv literal, hàm lib thật (xem _git)
        ["bash", "-c", f'source "{ADP_LIB}" && {cmd}'],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
    )


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "f.txt").write_text("x\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed")
    return repo


def test_state_authority_default_git_repo_is_trust_store(tmp_path: Path) -> None:
    """TH8: default flip — repo git ⇒ authority state sống ở $HOME/.adp/<repo-id>/.
    F1-đảo gốc: state trong repo thì agent (hoặc git checkout) sửa được chính
    bằng chứng đang gate mình."""
    repo = _git_repo(tmp_path)
    home = tmp_path / "fakehome"
    home.mkdir()
    rid = _lib_call(f'adp_repo_id "{repo}"')
    got = _lib_call(f'adp_state_dir "{repo}"', {"ADP_STATE_HOME": str(home)})
    assert got == f"{home}/.adp/{rid}", f"default phải là trust store, got {got!r}"


def test_state_authority_nongit_fallback_is_docs(tmp_path: Path) -> None:
    """TH8 chiều không-FAIL-oan: ngoài git (fixture trần) không có repo-id ⇒
    fallback docs/ như cũ — adp-decide fixture và tool đứng ngoài repo không vỡ."""
    assert _lib_call(f'adp_state_dir "{tmp_path}"') == f"{tmp_path}/docs"


def test_state_authority_store_init_writes_meta(tmp_path: Path) -> None:
    """TH8: adp_store_init tạo store + meta.json {origin, created_ts} — forensic
    khi đổi máy (SETUP copy ~/.adp) cần biết store này của repo nào."""
    repo = _git_repo(tmp_path)
    home = tmp_path / "fakehome"
    home.mkdir()
    store = _lib_call(f'adp_store_init "{repo}"', {"ADP_STATE_HOME": str(home)})
    meta = json.loads((Path(store) / "meta.json").read_text())
    assert meta["origin"] == str(repo)
    assert meta.get("created_ts"), "meta.json phải có created_ts"


def test_state_authority_migrate_moves_authority_files(tmp_path: Path) -> None:
    """TH8: adp_store_migrate dời 5 file authority từ docs/ vào store và GỠ bản
    gốc — file cũ ở lại là chỗ forge (F1 phép thử 1)."""
    repo = _git_repo(tmp_path)
    home = tmp_path / "fakehome"
    home.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    (docs / ".adp-audit.jsonl").write_text('{"ts": "t0", "gate": "seed"}\n')
    (docs / ".adp-red-proof.json").write_text('{"tasks": {}}\n')
    store = _lib_call(f'adp_store_migrate "{repo}"', {"ADP_STATE_HOME": str(home)})
    assert (Path(store) / ".adp-audit.jsonl").exists()
    assert (Path(store) / ".adp-red-proof.json").exists()
    assert not (docs / ".adp-audit.jsonl").exists(), "bản gốc docs/ phải được gỡ"
    assert not (docs / ".adp-red-proof.json").exists(), "bản gốc docs/ phải được gỡ"


def test_state_authority_forged_repo_redproof_not_read(tmp_path: Path) -> None:
    """TH8 F1 phép thử 2: red-proof forge đặt TRONG repo không được gate đọc —
    reader phải nhìn store, không nhìn docs/."""
    repo = _git_repo(tmp_path)
    home = tmp_path / "fakehome"
    home.mkdir()
    env = {"ADP_STATE_HOME": str(home)}
    _lib_call(f'adp_red_proof_record "{repo}" t1 sha_real 1 f.txt', env)
    docs = repo / "docs"
    docs.mkdir(exist_ok=True)
    (docs / ".adp-red-proof.json").write_text(
        json.dumps({"tasks": {"t1": {"acc_sha": "sha_FORGED", "red_exit": 1}}})
    )
    got = _lib_call(f'adp_red_proof_get "{repo}" t1 acc_sha', env)
    assert got == "sha_real", f"reader đọc nhầm bản forge trong repo: {got!r}"


def test_state_authority_audit_chain_prev_sha(tmp_path: Path) -> None:
    """TH8: audit hash-chain — mỗi dòng mang prev_sha = sha256 dòng trước
    (dòng đầu: genesis). Truncate/sửa giữa log là phát hiện được."""
    repo = _git_repo(tmp_path)
    home = tmp_path / "fakehome"
    home.mkdir()
    env = {"ADP_STATE_HOME": str(home)}
    _lib_call(f'adp_audit_event "{repo}" gate=g1 outcome=a', env)
    _lib_call(f'adp_audit_event "{repo}" gate=g2 outcome=b', env)
    store = _lib_call(f'adp_state_dir "{repo}"', env)
    lines = (Path(store) / ".adp-audit.jsonl").read_text().splitlines()
    first, second = json.loads(lines[0]), json.loads(lines[1])
    assert first["prev_sha"] == "genesis"
    want = hashlib.sha256(lines[0].encode()).hexdigest()
    assert second["prev_sha"] == want, "prev_sha phải là sha256 của dòng liền trước"


def test_state_authority_audit_chain_verify_detects_tamper(tmp_path: Path) -> None:
    """TH8 F1 phép thử 3: sửa giữa audit log ⇒ adp_audit_chain_verify rc≠0 kèm
    message; log sạch ⇒ rc0."""
    repo = _git_repo(tmp_path)
    home = tmp_path / "fakehome"
    home.mkdir()
    env = {"ADP_STATE_HOME": str(home)}
    for i in range(3):
        _lib_call(f'adp_audit_event "{repo}" gate=g{i} outcome=x', env)
    clean = _lib_rc(f'adp_audit_chain_verify "{repo}"', env)
    assert clean.returncode == 0, f"log sạch phải rc0: {clean.stderr!r}"
    store = _lib_call(f'adp_state_dir "{repo}"', env)
    audit = Path(store) / ".adp-audit.jsonl"
    lines = audit.read_text().splitlines()
    lines[1] = lines[1].replace('"outcome": "x"', '"outcome": "TAMPERED"')
    audit.write_text("\n".join(lines) + "\n")
    bad = _lib_rc(f'adp_audit_chain_verify "{repo}"', env)
    assert bad.returncode != 0, "tamper giữa log phải bị phát hiện"
    assert "chain" in (bad.stderr + bad.stdout).lower(), "message phải nêu chain-broken"


# ---------------------------------------------------------------------------
# TH9 spec 23 — HMAC authority (ADR trust-store p.3): key `~/.adp/key` NGOÀI mọi
# worktree ký red-proof / review artifact / state-hash / decision-pending; verify
# tại gate-verdict + adp-checkpoint + decision-gate. RED-first cho cả 3 điểm verify:
# F1b (red-proof viết tay đúng acc_sha vẫn PASS) và F1d (pending sửa/thay không bị
# coi là invalid) phải ĐẢO chiều. Mọi test route key qua ADP_STATE_HOME — không
# đụng store thật của máy.
# ---------------------------------------------------------------------------

ADP_BOOTSTRAP = Path(__file__).resolve().parent.parent / ".claude" / "tools" / "adp-bootstrap.sh"


def _hmac_home(tmp_path: Path, name: str = "hmac-home") -> dict[str, str]:
    """Fake HOME mang key HMAC thật (0600) — fixture của 'key ngoài repo'."""
    keydir = tmp_path / name / ".adp"
    keydir.mkdir(parents=True, exist_ok=True)
    key = keydir / "key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    return {"ADP_STATE_HOME": str(tmp_path / name)}


def test_hmac_stamp_verify_roundtrip(tmp_path: Path) -> None:
    """TH9: stamp ghi field `hmac` (64-hex, payload canonical sort_keys) và verify rc0.
    Đây là mặt PASS của DoD: artifact hợp lệ stamp bởi tool thật → PASS."""
    env = _hmac_home(tmp_path)
    art = tmp_path / "a.json"
    art.write_text(json.dumps({"verdict": "APPROVE", "model": "m"}))
    r = _lib_rc(f'adp_hmac_stamp "{art}"', env)
    assert r.returncode == 0, f"stamp với key phải rc0 — rc={r.returncode} err={r.stderr!r}"
    mac = json.loads(art.read_text()).get("hmac", "")
    assert len(mac) == 64, f"hmac phải là sha256 64-hex: {mac!r}"
    v = _lib_rc(f'adp_hmac_verify "{art}"', env)
    assert v.returncode == 0, f"verify artifact vừa stamp phải rc0 — rc={v.returncode}"


def test_hmac_verify_forge_matrix(tmp_path: Path) -> None:
    """TH9 DoD forge-matrix: đổi 1 byte payload / thiếu hmac / key sai ⇒ rc1 (invalid).
    rc1 ≠ rc2: invalid là forge, caller REFUSE; rc2 là thiếu key, caller strict/shadow."""
    env = _hmac_home(tmp_path)
    art = tmp_path / "a.json"
    art.write_text(json.dumps({"verdict": "APPROVE", "model": "m"}))
    assert _lib_rc(f'adp_hmac_stamp "{art}"', env).returncode == 0

    # (a) đổi 1 byte payload sau stamp
    obj = json.loads(art.read_text())
    obj["verdict"] = "APPROVe"
    art.write_text(json.dumps(obj))
    assert _lib_rc(f'adp_hmac_verify "{art}"', env).returncode == 1, (
        "payload đổi 1 byte mà verify vẫn rc0 — HMAC không bind payload"
    )

    # (b) thiếu hmac (artifact viết tay) — invalid, KHÔNG coi như hợp lệ
    art2 = tmp_path / "b.json"
    art2.write_text(json.dumps({"verdict": "APPROVE", "model": "m"}))
    assert _lib_rc(f'adp_hmac_verify "{art2}"', env).returncode == 1, (
        "artifact không có field hmac phải là invalid (rc1) khi máy CÓ key"
    )

    # (c) key sai (stamp bằng key khác) — chữ ký không chuyển nhượng được
    env2 = _hmac_home(tmp_path, name="other-home")
    art3 = tmp_path / "c.json"
    art3.write_text(json.dumps({"verdict": "APPROVE", "model": "m"}))
    assert _lib_rc(f'adp_hmac_stamp "{art3}"', env2).returncode == 0
    assert _lib_rc(f'adp_hmac_verify "{art3}"', env).returncode == 1, (
        "artifact ký bằng key KHÁC phải rc1 dưới key hiện hành"
    )


def test_hmac_missing_key_rc2(tmp_path: Path) -> None:
    """TH9: thiếu key là TRẠNG THÁI RIÊNG (rc2) — strict REFUSE, shadow warn+continue.
    Gộp nó vào rc1 là FAIL oan mọi repo chưa bootstrap key."""
    home = tmp_path / "empty-home"
    home.mkdir()
    env = {"ADP_STATE_HOME": str(home)}
    art = tmp_path / "a.json"
    art.write_text(json.dumps({"verdict": "APPROVE"}))
    assert _lib_rc(f'adp_hmac_stamp "{art}"', env).returncode == 2
    assert _lib_rc(f'adp_hmac_verify "{art}"', env).returncode == 2


def test_hmac_red_proof_record_stamps_entry(tmp_path: Path) -> None:
    """TH9: adp_red_proof_record (đường `adp-review.sh red`) tự stamp entry —
    'tool thật' để lại chữ ký mà record tay không làm giả được."""
    repo = _git_repo(tmp_path)
    env = _hmac_home(tmp_path)
    _lib_call(f'adp_red_proof_record "{repo}" t9 accsha 1 f.txt', env)
    proof = Path(_lib_call(f'adp_red_proof_path "{repo}"', env))
    entry = json.loads(proof.read_text())["t9"]
    assert len(entry.get("hmac", "")) == 64, f"entry phải mang hmac 64-hex: {entry!r}"
    v = _lib_rc(f'adp_hmac_verify "{proof}" t9', env)
    assert v.returncode == 0, f"entry do tool record phải verify rc0 — rc={v.returncode}"


def _hmac_gate_repo(tmp_path: Path, env: dict[str, str]) -> tuple[Path, str]:
    """Fixture gate-verdict PASS-baseline (như _gate_fixture) nhưng store/key theo env —
    CHỈ tính hợp lệ của RED-proof quyết định verdict trong cụm TH9."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed")
    (repo / "app.py").write_text("x = 2\n")
    acc = "exit 0"
    state = Path(_lib_call(f'adp_state_dir "{repo}"', env))
    state.mkdir(parents=True, exist_ok=True)
    (state / ".adp-exec-state.json").write_text(
        json.dumps(
            {
                "schema": "adp-handoff/v1",
                "task_id": "t-th9",
                "current_agent": "coder",
                "acceptance_cmd": acc,
                "risk_tier": "low",
                "parent_phase": "TH9",
                "files": ["app.py"],
            }
        )
        + "\n"
    )
    return repo, acc


def _run_gate_verdict_env(
    repo: Path, agent_type: str, env_extra: dict[str, str]
) -> dict[str, object]:
    env = dict(os.environ)
    env.pop("ADP_FORCE_SHADOW", None)
    env.pop("ADP_STRICT", None)
    env.update(env_extra)
    subprocess.run(  # noqa: S603 — argv literal, hook thật trên fixture (xem _git)
        ["bash", str(GATE_VERDICT)],  # noqa: S607
        input=json.dumps({"cwd": str(repo), "agent_type": agent_type}),
        capture_output=True,
        text=True,
        env=env,
    )
    artifact = Path(_lib_call(f'adp_gate_verdict_path "{repo}"', env_extra))
    assert artifact.is_file(), "gate-verdict phải ghi artifact snapshot"
    return json.loads(artifact.read_text())


def test_hmac_gate_verdict_fails_forged_redproof(tmp_path: Path) -> None:
    """TH9 F1b ĐẢO (điểm verify 1/3): red-proof VIẾT TAY đúng acc_sha + red_exit≠0 —
    kịch bản audit 2026-07-28 từng cho PASS — giờ phải FAIL vì thiếu chữ ký tool."""
    env = _hmac_home(tmp_path)
    repo, acc = _hmac_gate_repo(tmp_path, env)
    proof = Path(_lib_call(f'adp_red_proof_path "{repo}"', env))
    proof.write_text(
        json.dumps({"t-th9": {"acc_sha": _cmd_sha(acc), "red_exit": "1", "files": "app.py"}})
    )
    obj = _run_gate_verdict_env(repo, "coder", env)
    assert obj.get("verdict") == "FAIL", (
        f"forge F1b phải FAIL khi máy có key — artifact thật: {obj!r}"
    )
    assert "red-proof-unverified" in str(obj.get("flags", "")), (
        f"flag phải nêu đúng nguyên nhân (red-proof-unverified): {obj!r}"
    )


def test_hmac_gate_verdict_passes_tool_stamped_redproof(tmp_path: Path) -> None:
    """Chiều khoá TH9: RED-proof do TOOL record (stamp kèm) → PASS như cũ —
    verify sai một ly là REFUSE oan mọi micro-task."""
    env = _hmac_home(tmp_path)
    repo, acc = _hmac_gate_repo(tmp_path, env)
    accsha = _cmd_sha(acc)
    _lib_call(f'adp_red_proof_record "{repo}" t-th9 "{accsha}" 1 app.py', env)
    obj = _run_gate_verdict_env(repo, "coder", env)
    assert obj.get("verdict") == "PASS", (
        f"red-proof tool-stamped + mọi gate xanh phải PASS: {obj!r}"
    )


def test_hmac_gate_verdict_no_key_shadow_no_false_fail(tmp_path: Path) -> None:
    """Chiều không-FAIL-oan: máy KHÔNG có key (chưa bootstrap) ⇒ shadow — proof
    viết tay vẫn PASS như trước TH9 (warn, không block); strict mới REFUSE."""
    home = tmp_path / "nokey-home"
    home.mkdir()
    env = {"ADP_STATE_HOME": str(home)}
    repo, acc = _hmac_gate_repo(tmp_path, env)
    proof = Path(_lib_call(f'adp_red_proof_path "{repo}"', env))
    proof.write_text(
        json.dumps({"t-th9": {"acc_sha": _cmd_sha(acc), "red_exit": "1", "files": "app.py"}})
    )
    obj = _run_gate_verdict_env(repo, "coder", env)
    assert obj.get("verdict") == "PASS", (
        f"không key = shadow, không được FAIL oan hành vi cũ: {obj!r}"
    )


def test_hmac_review_stamp_signs_artifact(tmp_path: Path) -> None:
    """TH9 (điểm verify 2/3, mặt stamp): `adp-review.sh stamp` ký artifact; sửa
    verdict SAU stamp ⇒ verify rc1 — checkpoint sẽ REFUSE (case spine [TH9])."""
    env_extra = _hmac_home(tmp_path)
    repo = _git_repo(tmp_path)
    (repo / "f.txt").write_text("y\n")
    src = tmp_path / "vin.json"
    out = tmp_path / "art.json"
    src.write_text(json.dumps({"verdict": "APPROVE", "model": "claude-haiku-4-5"}))
    env = dict(os.environ)
    env.update(env_extra)
    r = subprocess.run(  # noqa: S603 — argv literal, tool thật trên fixture (xem _git)
        ["bash", str(ADP_REVIEW), "stamp", str(repo), str(src), str(out)],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, f"stamp phải pass — stderr: {r.stderr!r}"
    assert len(json.loads(out.read_text()).get("hmac", "")) == 64, (
        "stamp với key phải ghi hmac vào artifact"
    )
    assert _lib_rc(f'adp_hmac_verify "{out}"', env_extra).returncode == 0
    obj = json.loads(out.read_text())
    obj["verdict"] = "APPROVE-FORGED"
    out.write_text(json.dumps(obj))
    assert _lib_rc(f'adp_hmac_verify "{out}"', env_extra).returncode == 1, (
        "sửa verdict sau stamp phải làm verify rc1"
    )


def test_hmac_state_hash_stamp_append_and_verify(tmp_path: Path) -> None:
    """TH9: GIÁ TRỊ .adp-state-hash được ký theo dòng — sửa stamp cho khớp state
    đã drift (kịch bản ADR p.3 nhóm 2) phải bị verify rc1; dòng legacy tolerate."""
    env = _hmac_home(tmp_path)
    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    line = "abc123def456 @ 2026-07-29T00:00 23-Task phase-TH9 DONE"
    assert _lib_rc(f'adp_state_stamp_append "{root}" "{line}"', env).returncode == 0
    content = (root / "docs" / ".adp-state-hash").read_text().strip()
    assert " hmac=" in content, f"dòng stamp phải mang hmac=: {content!r}"
    assert _lib_rc(f'adp_state_stamp_verify "{root}"', env).returncode == 0

    # forge: đổi giá trị hash đầu dòng, giữ nguyên hmac cũ
    forged = content.replace("abc123def456", "eeeeeeeeeeee")
    (root / "docs" / ".adp-state-hash").write_text(forged + "\n")
    assert _lib_rc(f'adp_state_stamp_verify "{root}"', env).returncode == 1, (
        "state-hash forge (đổi giá trị, giữ hmac) phải rc1"
    )

    # legacy: dòng không có hmac= (trước TH9) → rc0, không REFUSE oan lịch sử
    (root / "docs" / ".adp-state-hash").write_text("legacyhash @ ts spec phase DONE\n")
    assert _lib_rc(f'adp_state_stamp_verify "{root}"', env).returncode == 0


def _run_decision_gate(repo: Path, env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("ADP_FORCE_SHADOW", None)
    env.pop("ADP_STRICT", None)
    env.pop("ADP_DECISION_ACTIVE", None)
    env.update(env_extra)
    return subprocess.run(  # noqa: S603 — argv literal, hook thật trên fixture (xem _git)
        ["bash", str(DECISION_GATE)],  # noqa: S607
        input=json.dumps({"cwd": str(repo)}),
        capture_output=True,
        text=True,
        env=env,
    )


def test_hmac_decision_gate_fake_resolved_blocks(tmp_path: Path) -> None:
    """TH9 F1d ĐẢO (điểm verify 3/3): pending 'đã resolve' viết tay (không HMAC)
    dưới gate ACTIVE phải bị coi là INVALID → block rc2, KHÔNG coi như resolved."""
    docs = tmp_path / "docs"
    docs.mkdir()
    pend = docs / ".adp-decision-pending.json"
    _decision_artifact(pend, chosen="A")  # fake-resolve, không qua adp-decide
    env = _hmac_home(tmp_path)
    env["ADP_DECISION_ACTIVE"] = "1"
    r = _run_decision_gate(tmp_path, env)
    assert r.returncode == 2, (
        f"fake-resolved không HMAC + ACTIVE phải block rc2 — rc={r.returncode} out={r.stdout!r}"
    )
    assert "hmac" in (r.stderr + r.stdout).lower(), (
        f"message phải nêu lý do integrity/hmac: {r.stderr!r}"
    )


def test_hmac_decision_gate_valid_resolved_continues(tmp_path: Path) -> None:
    """Chiều khoá F1d: resolved + HMAC hợp lệ ⇒ continue rc0 — verify sai một ly
    là mọi decision đã resolve hợp lệ bị block vĩnh viễn."""
    docs = tmp_path / "docs"
    docs.mkdir()
    pend = docs / ".adp-decision-pending.json"
    _decision_artifact(pend, chosen="A")
    env = _hmac_home(tmp_path)
    assert _lib_rc(f'adp_hmac_stamp "{pend}"', env).returncode == 0
    env["ADP_DECISION_ACTIVE"] = "1"
    r = _run_decision_gate(tmp_path, env)
    assert r.returncode == 0, f"resolved+stamped phải continue — rc={r.returncode}"
    assert '"continue": true' in r.stdout


def test_hmac_decision_gate_open_still_blocks_active(tmp_path: Path) -> None:
    """Chiều khoá: decision OPEN (chosen=null) dưới ACTIVE vẫn block như cũ —
    HMAC không mở đường tắt nào cho decision chưa resolve."""
    docs = tmp_path / "docs"
    docs.mkdir()
    _decision_artifact(docs / ".adp-decision-pending.json", chosen=None)
    env = _hmac_home(tmp_path)
    env["ADP_DECISION_ACTIVE"] = "1"
    r = _run_decision_gate(tmp_path, env)
    assert r.returncode == 2, f"open decision + ACTIVE phải block — rc={r.returncode}"


def test_hmac_decision_gate_no_key_modes(tmp_path: Path) -> None:
    """Thiếu key: shadow honor resolved (không DoS repo chưa bootstrap);
    ADP_STRICT=1 ⇒ fail-closed block."""
    docs = tmp_path / "docs"
    docs.mkdir()
    _decision_artifact(docs / ".adp-decision-pending.json", chosen="A")
    home = tmp_path / "nokey-home"
    home.mkdir()
    base = {"ADP_STATE_HOME": str(home), "ADP_DECISION_ACTIVE": "1"}
    r = _run_decision_gate(tmp_path, dict(base))
    assert r.returncode == 0, f"no key + shadow phải honor resolved — rc={r.returncode}"
    r2 = _run_decision_gate(tmp_path, {**base, "ADP_STRICT": "1"})
    assert r2.returncode == 2, f"no key + strict phải fail-closed — rc={r2.returncode}"


def test_hmac_bootstrap_generates_key(tmp_path: Path) -> None:
    """TH9: adp-bootstrap sinh key 0600 tại <state-home>/.adp/key — idempotent,
    không bao giờ nằm trong repo nào."""
    proj = _git_repo(tmp_path, name="proj")
    (proj / "pyproject.toml").write_text("[project]\nname='p'\n")
    home = tmp_path / "boot-home"
    home.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "ADP_STATE_HOME": str(home),
            "ADP_SETTINGS_FILE": str(tmp_path / "settings.json"),
        }
    )
    r = subprocess.run(  # noqa: S603 — argv literal, tool thật trên fixture (xem _git)
        ["bash", str(ADP_BOOTSTRAP), str(proj)],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, f"bootstrap phải pass — out={r.stdout!r} err={r.stderr!r}"
    key = home / ".adp" / "key"
    assert key.is_file(), "bootstrap phải sinh key HMAC"
    assert (key.stat().st_mode & 0o777) == 0o600, "key phải 0600"
    first = key.read_bytes()
    r2 = subprocess.run(  # noqa: S603 — argv literal (xem trên)
        ["bash", str(ADP_BOOTSTRAP), str(proj)],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
    )
    assert r2.returncode == 0
    assert key.read_bytes() == first, "bootstrap lần 2 KHÔNG được xoay key (idempotent)"


def test_hmac_decide_resolve_stamps_pending(tmp_path: Path) -> None:
    """TH9 (deviation ghi ở smoke 23-TH9): resolve là điểm stamp duy nhất của flow
    quyết định thật — thiếu nó, decision-gate promoted sẽ false-block mọi resolve
    hợp lệ (đúng lỗ F1d phase này đóng)."""
    f = tmp_path / "d.json"
    _decision_artifact(f, chosen=None)
    env_extra = _hmac_home(tmp_path)
    env = dict(os.environ)
    env.update(env_extra)
    r = subprocess.run(  # noqa: S603 — argv literal, tool thật trên fixture (xem _git)
        ["bash", str(ADP_DECIDE), "resolve", str(f), "A", "wyatt"],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, f"resolve phải pass — stderr: {r.stderr!r}"
    assert len(json.loads(f.read_text()).get("hmac", "")) == 64, (
        "resolve phải stamp HMAC vào artifact"
    )
    assert _lib_rc(f'adp_hmac_verify "{f}"', env_extra).returncode == 0
