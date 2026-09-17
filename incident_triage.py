"""Read one incident report from Box and turn it into a triage decision with Jev."""

from __future__ import annotations

import argparse
import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import msgspec
from box_sdk_gen import (
    BoxClient,
    BoxDeveloperTokenAuth,
    FetchOptions,
    ResponseFormat,
    UploadFileAttributes,
    UploadFileAttributesParentField,
)
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient


BASE_DIR = Path(__file__).resolve().parent

QUESTIONS = {
    "incident_type": Choice(
        instructions="What is the primary type of incident described in the report?",
        criteria={
            "availability": "A service or feature is unavailable or degraded.",
            "security_or_privacy": "There may be unauthorized access, exposure, or misuse of data.",
            "data_integrity": "Records, transactions, or other data may be incorrect, lost, or duplicated.",
            "other": "The report does not primarily describe one of the other incident types.",
        },
    ),
    "severity": Score(
        instructions="How severe is the incident based on its customer impact and unresolved risk?",
        criteria=[
            "Limited impact with a clear workaround and no meaningful unresolved risk.",
            "Multiple customers are affected, service is degraded, or the investigation is incomplete.",
            "Broad customer impact, possible data exposure, material data loss, or an urgent unresolved risk.",
        ],
    ),
    "needs_escalation": Noul(
        instructions="Does this report require immediate security, privacy, or executive escalation?",
        criteria={
            "true": "There is credible evidence of possible data exposure, material data integrity risk, broad impact, or an unresolved issue that should not wait for routine handling.",
            "false": "The report describes a contained, understood incident that can remain in routine operational handling.",
        },
    ),
}


def load_env_file() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Set {name} before running the demo.")
    return value


def box_client() -> BoxClient:
    return BoxClient(BoxDeveloperTokenAuth(required("BOX_DEVELOPER_TOKEN")))


def find_report(client: BoxClient, folder_id: str, file_name: str) -> tuple[str, str]:
    for item in client.folders.get_folder_items(folder_id, limit=100).entries:
        if item.type == "file" and item.name == file_name:
            return item.id, item.name
    raise RuntimeError(f"Could not find {file_name!r} in Box folder {folder_id}.")


def download_markdown(client: BoxClient, file_id: str) -> str:
    """Ask Box for its Markdown representation and download that representation."""
    for _ in range(10):
        file = client.files.get_file_by_id(
            file_id,
            fields=["id", "name", "representations"],
            x_rep_hints="[markdown]",
        )
        representations = getattr(file.representations, "entries", []) if file.representations else []
        markdown = next(
            (entry for entry in representations if entry.representation == "markdown"),
            None,
        )
        if markdown is None:
            raise RuntimeError("Box did not return a Markdown representation for this file.")

        status = getattr(getattr(markdown, "status", None), "state", None)
        status = getattr(status, "value", status)
        if status == "none":
            info_url = getattr(getattr(markdown, "info", None), "url", None)
            if not info_url:
                raise RuntimeError("Box returned no URL to start Markdown generation.")
            client.make_request(FetchOptions(info_url, "GET"))
        elif status in {"success", "viewable"}:
            template = getattr(getattr(markdown, "content", None), "url_template", None)
            if not template:
                raise RuntimeError("Box returned no Markdown download URL.")
            response = client.make_request(
                FetchOptions(
                    template.replace("{+asset_path}", ""),
                    "GET",
                    response_format=ResponseFormat.BINARY,
                )
            )
            if response.content is None:
                raise RuntimeError("Box returned no Markdown content.")
            return response.content.read().decode("utf-8-sig")

        time.sleep(2)

    raise RuntimeError("Box did not finish generating the Markdown representation in time.")


def answer_payload(answer: object) -> dict:
    # msgspec tagged structs put `type` on the wire, not as a Python attribute.
    return msgspec.to_builtins(answer)


def decide(answers: dict) -> str:
    escalation_probability = answers["needs_escalation"].noul
    severity = answers["severity"]
    incident_type = answers["incident_type"]

    # Jev supplies semantic signals. This small policy remains ordinary code.
    if escalation_probability >= 0.75:
        return "ESCALATE"
    if severity.score >= 1.0 or severity.confidence < 0.60 or incident_type.confidence < 0.60:
        return "REVIEW"
    return "MONITOR"


def decision_card(file_name: str, response: object, decision: str) -> str:
    answers = response.answers
    incident_type = answers["incident_type"]
    severity = answers["severity"]
    escalation = answers["needs_escalation"]
    return "\n".join(
        [
            f"# Incident triage: {file_name}",
            "",
            f"Decision: **{decision}**",
            "",
            "| Signal | Result | Confidence / probability |",
            "| --- | --- | --- |",
            f"| Incident type | `{incident_type.choice}` | {incident_type.confidence:.2f} confidence |",
            f"| Severity | `{severity.score:.2f}` | {severity.confidence:.2f} confidence |",
            f"| Needs escalation | `{escalation.noul:.2f}` | probability of yes |",
            "",
            "This is a triage signal for human follow-up, not a final security, legal, or customer-impact determination.",
        ]
    ) + "\n"


def write_back(client: BoxClient, folder_id: str, source_name: str, card: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_name = f"{Path(source_name).stem}-triage-{stamp}.md"
    attributes = UploadFileAttributes(
        name=output_name,
        parent=UploadFileAttributesParentField(id=folder_id),
    )
    client.uploads.upload_file(
        attributes,
        io.BytesIO(card.encode("utf-8")),
        file_file_name=output_name,
        file_content_type="text/markdown",
    )
    return output_name


def run(args: argparse.Namespace) -> None:
    if args.local:
        file_name = Path(args.local).name
        source_text = Path(args.local).read_text(encoding="utf-8")
        client = None
        folder_id = ""
    else:
        client = box_client()
        folder_id = required("BOX_FOLDER_ID")
        file_name = os.environ.get("BOX_FILE_NAME", "incident-report.pdf")
        file_id, file_name = find_report(client, folder_id, file_name)
        source_text = download_markdown(client, file_id)

    with TypeSafeClient() as typesafe:
        response = typesafe.system_one(
            state={"document_markdown": source_text},
            questions=QUESTIONS,
        )

    decision = decide(response.answers)
    card = decision_card(file_name, response, decision)
    print(card)
    print(json.dumps({name: answer_payload(answer) for name, answer in response.answers.items()}, indent=2, default=str))

    if args.write_back:
        if client is None:
            raise RuntimeError("--write-back requires a Box source document.")
        output_name = write_back(client, folder_id, file_name, card)
        print(f"Saved decision card to Box as {output_name}")


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local",
        help="Use a local Markdown fixture instead of requesting the representation from Box.",
    )
    parser.add_argument(
        "--write-back",
        action="store_true",
        help="Upload the decision card to the same Box folder.",
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
