from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _requirements(name: str) -> list[str]:
    return [
        line.strip()
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_s3_artifact_profile_is_minimal_and_tracks_runtime_pin() -> None:
    minimal = _requirements("requirements-s3-artifact.txt")
    runtime = _requirements("requirements.txt")

    assert minimal == ["boto3==1.43.115"]
    assert minimal[0] in runtime
