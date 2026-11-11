"""dev 자동 반영: 앱 저장소 브랜치의 검증된 새 이미지를 찾아 values-dev 태그를 바꾸는 PR을 만들고 바로 병합한다.

V2 auto-release(Cloud 레포 scripts/v2_common.py, v2_release.py)를 옮긴 것이다. 클러스터 반영은 Argo CD가 한다.
  - 대상: release/dev.json의 그룹(앱 저장소·브랜치·CI workflow·게시 job·ECR 저장소·values 파일)
  - 조건: 브랜치 head 커밋의 최신 push CI가 성공했고, 게시 job이 성공했고, ECR에 그 SHA 태그가 있다
  - EventBridge가 ECR push 이벤트와 10분 주기로 auto-release-dev workflow를 실행한다(Cloud 레포
    infrastructure/v3-gitops-dispatch.yaml). push 이벤트는 CI가 끝나기 직전에 오므로 CI가 진행 중이면
    CI_WAIT_SECONDS 동안 다시 확인하고, 넘기면 다음 실행이 처리한다.

미리 보기(조회만): gh auth login, AWS 자격 증명을 갖춘 뒤 python scripts/release.py
실제 반영(--execute)은 GitHub Actions의 main에서만 한다.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "release" / "dev.json"
SHA = re.compile(r"[0-9a-f]{40}")
# dev가 운영 브랜치를 따라가면 검증 전 코드가 아니라 운영 코드가 dev에 올라간다(V2와 같은 규칙).
PRODUCTION_BRANCHES = {"main", "feat/v1"}


class Error(RuntimeError):
    pass


class Pending(Error):
    """ECR push 이벤트가 앱 CI 완료보다 먼저 왔다."""


def run(args, *, check=True):
    try:
        result = subprocess.run(args, text=True, encoding="utf-8", capture_output=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as error:
        raise Error(f"{args[0]} {args[1]}: 실행할 수 없거나 시간 초과") from error
    if check and result.returncode:
        # 입력값·자격 증명이 로그에 남지 않게 명령 이름과 종료 코드만 남긴다.
        raise Error(f"{args[0]} {args[1]}: 실패 (exit {result.returncode}) {result.stderr.strip()[:200]}")
    return result.stdout


def gh_get(path):
    return json.loads(run(["gh", "api", path]))


def aws(*args):
    output = run(["aws", *args, "--region", CFG["region"], "--output", "json", "--no-cli-pager"])
    return json.loads(output) if output.strip() else {}


def load_config():
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("environment") != "dev" \
            or not re.fullmatch(r"\d{12}", data.get("account_id", "")) or not data.get("groups"):
        raise Error("release/dev.json 형식이 맞지 않는다")
    for name, group in data["groups"].items():
        if set(group) != {"repository", "branch", "workflow", "job", "ecr_repository", "values"}:
            raise Error(f"{name}: 설정 키가 맞지 않는다")
        if group["branch"] in PRODUCTION_BRANCHES:
            raise Error(f"{name}: dev는 운영 브랜치({group['branch']})를 따라갈 수 없다")
        if not re.fullmatch(r"keepgo-[a-z0-9-]+", group["ecr_repository"]):
            raise Error(f"{name}: ECR 저장소 이름이 맞지 않는다")
        for path in group["values"]:
            if not path.startswith("apps/") or not path.endswith("-dev.yaml") or not (ROOT / path).is_file():
                raise Error(f"{name}: dev values 파일이 아니거나 없다: {path}")
    return data


def current_tag(path):
    """values 파일의 image 블록 첫 tag(set-image-tag.sh와 같은 규칙)."""
    in_image = False
    for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        if re.match(r"^image:\s*$", line):
            in_image = True
        elif in_image and re.match(r"^[^\s#]", line):
            in_image = False
        elif in_image:
            match = re.match(r"^\s+tag:\s*\"?([^\"\s]+)\"?", line)
            if match:
                return match.group(1)
    raise Error(f"image.tag를 찾지 못함: {path}")


def jobs(repository, run_id, attempt):
    found, page = [], 1
    while True:
        data = gh_get(f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")
        found.extend(data["jobs"])
        if len(data["jobs"]) < 100:
            return found
        page += 1


def successful_run(group, commit):
    """commit의 최신 push CI가 성공했고 게시 job도 성공했으면 그 실행을, 아니면 None. 진행 중이면 Pending."""
    repo, branch = group["repository"], quote(group["branch"], safe="")
    data = gh_get(f'repos/{repo}/actions/workflows/{group["workflow"]}/runs'
                  f'?branch={branch}&event=push&head_sha={commit}&per_page=100')
    if not data["workflow_runs"]:
        return None
    latest = max(data["workflow_runs"], key=lambda item: item["id"])
    if latest.get("status") != "completed":
        raise Pending("CI 진행 중")
    if latest.get("conclusion") != "success":
        return None
    if latest.get("head_sha") != commit or latest.get("head_branch") != group["branch"] or latest.get("event") != "push":
        raise Error("CI 출처가 맞지 않는다")
    if not any(job["name"] == group["job"] and job.get("conclusion") == "success"
               for job in jobs(repo, latest["id"], latest["run_attempt"])):
        return None  # workflow는 성공했지만 게시 job이 skip됨
    return latest


def image_exists(group, commit):
    try:
        images = aws("ecr", "describe-images", "--repository-name", group["ecr_repository"],
                     "--image-ids", f"imageTag={commit}")["imageDetails"]
    except Error as error:
        if "ImageNotFound" in str(error):
            return False
        raise
    return len(images) == 1


def candidate(group):
    commit = gh_get(f'repos/{group["repository"]}/commits/{quote(group["branch"], safe="")}')["sha"]
    if not SHA.fullmatch(commit):
        raise Error("브랜치 head가 커밋 SHA가 아니다")
    if all(current_tag(path) == commit for path in group["values"]):
        return commit, None, "latest"
    ci = successful_run(group, commit)
    if ci is None:
        return commit, None, "no-image"
    if not image_exists(group, commit):
        raise Error(f"CI는 성공했지만 ECR {group['ecr_repository']}에 {commit[:12]} 태그가 없다")
    return commit, ci, "new"


def publish(name, group, commit, ci):
    """최신 main에서 release 브랜치를 만들어 태그만 바꾸고 PR을 만들어 squash 병합한다."""
    branch = f'release/dev/{name}-{commit[:12]}-{os.environ["GITHUB_RUN_ID"]}-{os.environ.get("GITHUB_RUN_ATTEMPT", "1")}'
    run(["git", "fetch", "--quiet", "origin", "main"])
    run(["git", "checkout", "--quiet", "-B", branch, "origin/main"])
    run(["bash", "scripts/set-image-tag.sh", commit, *group["values"]])
    if not run(["git", "status", "--porcelain", "--", *group["values"]]).strip():
        return False  # 그 사이 다른 실행이 이미 반영했다
    title = f"release(dev): {name} {commit[:12]}"
    run(["git", "add", "--", *group["values"]])
    run(["git", "commit", "--quiet", "-m", title, "-m", f'source: {group["repository"]}@{commit}'])
    head = run(["git", "rev-parse", "HEAD"]).strip()
    run(["git", "push", "--quiet", "origin", branch])
    body = (f'dev 자동 반영. Source: {group["repository"]}@{group["branch"]} `{commit}`\n'
            f'CI: {ci["html_url"]}\n'
            f'Image: `{CFG["account_id"]}.dkr.ecr.{CFG["region"]}.amazonaws.com/{group["ecr_repository"]}:{commit}`\n\n'
            'auto-release-dev workflow(scripts/release.py)가 만들고 병합했다. 반영은 Argo CD가 한다.\n')
    with tempfile.TemporaryDirectory() as directory:
        body_file = Path(directory) / "body.md"
        body_file.write_text(body, encoding="utf-8")
        pr_url = run(["gh", "pr", "create", "--base", "main", "--head", branch,
                      "--title", title, "--body-file", str(body_file)]).strip()
    run(["gh", "pr", "merge", pr_url, "--squash", "--delete-branch", "--match-head-commit", head])
    if json.loads(run(["gh", "pr", "view", pr_url, "--json", "state"]))["state"] != "MERGED":
        raise Error("PR 병합이 끝나지 않았다")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="PR을 만들고 병합한다(GitHub Actions main 전용)")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.execute and (os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REF") != "refs/heads/main"):
        raise Error("실제 반영은 GitHub Actions의 main에서만 한다. 로컬에서는 --execute 없이 미리 본다")
    if aws("sts", "get-caller-identity")["Account"] != CFG["account_id"]:
        raise Error("AWS 계정이 다르다")
    if args.execute:
        run(["git", "config", "user.name", "github-actions[bot]"])
        run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    errors, released = [], []

    def release_one(name):
        group = CFG["groups"][name]
        try:
            commit, ci, state = candidate(group)
            if state == "latest":
                print(f"{name}: 최신 ({commit[:12]})")
            elif state == "no-image":
                print(f"{name}: {commit[:12]}의 이미지가 없다 (CI 실패·게시 생략·CI 없음)")
            elif not args.execute:
                print(f'{name}: {commit[:12]} 반영 대상 (미리 보기라 PR을 만들지 않음) {ci["html_url"]}')
            elif publish(name, group, commit, ci):
                released.append(f"{name} {commit[:12]}")
                print(f"{name}: {commit[:12]} 병합")
            else:
                print(f"{name}: {commit[:12]} 이미 반영됨")
        except Pending:
            print(f"{name}: CI 진행 중. 다른 그룹을 처리한 뒤 다시 확인한다")
            return True
        except Error as error:
            errors.append(name)
            print(f"{name}: {error}", file=sys.stderr)
        return False

    waiting = [name for name in CFG["groups"] if release_one(name)]
    deadline = time.monotonic() + int(os.environ.get("CI_WAIT_SECONDS", "180"))
    while waiting and time.monotonic() < deadline:
        time.sleep(min(15, max(0, deadline - time.monotonic())))
        waiting = [name for name in waiting if release_one(name)]
    if waiting:
        print("CI 대기 상한을 넘겼다. 다음 실행이 처리한다: " + ", ".join(waiting))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("### dev 자동 반영\n" + ("\n".join(f"- {item}" for item in released) or "- 바뀐 것 없음") + "\n")
    if errors:
        raise Error("반영 실패: " + ", ".join(errors))
    return 0


if __name__ == "__main__":
    try:
        CFG = load_config()
        sys.exit(main())
    except Error as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
