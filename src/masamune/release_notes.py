from __future__ import annotations

from html import escape
from pathlib import Path
from urllib.parse import quote


class ReleaseError(RuntimeError):
    """Raised when release notes cannot be rendered safely."""


def render_release_notes(
    summary: dict[str, object], *, repository: str | None = None, tag: str | None = None
) -> str:
    jobs = summary.get("jobs")
    if not isinstance(jobs, list):
        raise ReleaseError("invalid build summary")
    lines = [
        "# Morphe release",
        "",
        str(summary.get("summary", "Verified build.")),
        "",
        "## Builds",
        "",
        "| App | Version | Architecture | Provider | Non-root APK | Root APK | Root module |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    patch_apps: dict[tuple[str, str], list[str]] = {}
    applied: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], bool, bool]] = {}
    for job in jobs:
        if not isinstance(job, dict) or not isinstance(
            job.get("artifacts"), (list, tuple)
        ):
            raise ReleaseError("invalid build summary")
        artifacts = job["artifacts"]
        if not all(isinstance(item, str) for item in artifacts):
            raise ReleaseError("invalid build summary")
        non_root = next(
            (
                item
                for item in artifacts
                if item.endswith(".apk") and "-root.apk" not in item
            ),
            None,
        )
        root = next((item for item in artifacts if item.endswith("-root.apk")), None)
        module = next((item for item in artifacts if item.endswith(".zip")), None)
        name = str(job.get("name") or job.get("package"))
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    name,
                    job.get("version_name"),
                    job.get("architecture"),
                    _provider_label(job.get("provider")),
                    _artifact_cell(non_root, repository, tag),
                    _artifact_cell(root, repository, tag),
                    _artifact_cell(module, repository, tag),
                )
            )
            + " |"
        )
        repository_name = job.get("patches_repository")
        patches_tag = job.get("patches_tag")
        if (
            isinstance(repository_name, str)
            and isinstance(patches_tag, str)
            and repository_name
            and patches_tag
        ):
            patch_apps.setdefault((repository_name, patches_tag), []).append(name)
        selected = _patch_list(job.get("selected_patches", ()))
        root_patches = _patch_list(job.get("root_patches", ()))
        package = str(job.get("package") or name)
        applied.setdefault(
            package,
            (
                name,
                selected,
                root_patches,
                non_root is not None,
                root is not None or module is not None,
            ),
        )
    skipped = summary.get("skipped") or []
    if not isinstance(skipped, list):
        raise ReleaseError("invalid build summary")
    for entry in skipped:
        if not isinstance(entry, dict):
            raise ReleaseError("invalid build summary")
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    entry.get("name") or entry.get("package"),
                    entry.get("version_name"),
                    entry.get("architecture"),
                    "—",
                    "—",
                    "—",
                    "—",
                )
            )
            + " |"
        )
    lines.extend(("", "## Applied patches", ""))
    for name, patches, root_patches, has_non_root, has_root in sorted(applied.values()):
        counts = []
        if has_non_root:
            counts.append(f"{len(patches)} non-root")
        if has_root:
            counts.append(f"{len(root_patches)} root")
        lines.extend(
            (
                "<details>",
                f"<summary>📦 <strong>{escape(name)}</strong> • {', '.join(counts)} patches</summary>",
                "",
            )
        )
        if has_non_root:
            lines.extend(_patches_section("Non-root APK", patches))
        if has_root:
            lines.extend(_patches_section("Root APK/module", root_patches))
        lines.extend(("</details>", ""))
    if any(entry[3] for entry in applied.values()):
        lines.extend(
            (
                "",
                "## Obtainium",
                "",
                (
                    "Import [obtainium.json]"
                    f"({_artifact_url('obtainium.json', repository, tag)}) in "
                    "[Obtainium](https://github.com/ImranR98/Obtainium) "
                    "to install and receive updates for all non-root APKs."
                    if repository and tag
                    else "Release includes `obtainium.json` for [Obtainium](https://github.com/ImranR98/Obtainium) imports."
                ),
            )
        )
    lines.extend(
        (
            "",
            "## Install requirements",
            "",
            "- Non-root APKs require [Morphe MicroG-RE](https://github.com/MorpheApp/MicroG-RE/).",
            "- Root module users may want [zygisk-detach](https://github.com/j-hc/zygisk-detach) so Play Store does not offer updates for the patched app.",
            "",
            "## Patch bundles",
            "",
            "| Apps | Patch bundle | Release changelog |",
            "| --- | --- | --- |",
        )
    )
    for (repository_name, patches_tag), apps in sorted(patch_apps.items()):
        url = f"https://github.com/{repository_name}/releases/tag/{quote(patches_tag, safe='')}"
        lines.append(
            f"| {_markdown_cell(', '.join(sorted(set(apps))))} | "
            f"`{_markdown_cell(repository_name)}` `{_markdown_cell(patches_tag)}` | "
            f"[View changelog]({url}) |"
        )
    return "\n".join(lines) + "\n"


def _artifact_cell(
    artifact: str | None, repository: str | None, tag: str | None
) -> str:
    if artifact is None:
        return "—"
    name = Path(artifact).name
    if repository and tag:
        return f"[Download]({_artifact_url(name, repository, tag)})"
    return f"`{_markdown_cell(name)}`"


def _artifact_url(name: str, repository: str, tag: str) -> str:
    return (
        f"https://github.com/{repository}/releases/download/"
        f"{quote(tag, safe='')}/{quote(name, safe='')}"
    )


def _provider_label(value: object) -> str:
    if not isinstance(value, str):
        return "—"
    return {"local": "Local APKs"}.get(value, "—")


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _patch_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ReleaseError("invalid selected patches")
    return tuple(value)


def _patches_section(label: str, patches: tuple[str, ...]) -> tuple[str, ...]:
    heading = f"### {label} ({len(patches)})"
    return (heading, "", *(f"- {_inline_code(patch)}" for patch in patches), "")


def _inline_code(value: object) -> str:
    return "`" + _markdown_cell(value).replace("`", "\\`") + "`"

