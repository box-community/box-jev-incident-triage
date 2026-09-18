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
    BoxAPIError,
    BoxClient,
    BoxDeveloperTokenAuth,
    CreateFileMetadataByIdScope,
    CreateMetadataTemplateFields,
    CreateMetadataTemplateFieldsOptionsField,
    CreateMetadataTemplateFieldsTypeField,
    CreateTaskAction,
    CreateTaskAssignmentAssignTo,
    CreateTaskAssignmentTask,
    CreateTaskCompletionRule,
    CreateTaskItem,
    CreateTaskItemTypeField,
    FetchOptions,
    GetFileMetadataByIdScope,
    ResponseFormat,
    UpdateFileByIdParent,
    UploadFileAttributes,
    UploadFileAttributesParentField,
    UpdateFileMetadataByIdRequestBody,
    UpdateFileMetadataByIdRequestBodyOpField,
    UpdateFileMetadataByIdScope,
)
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_METADATA_TEMPLATE_KEY = "jevIncidentTriage"
METADATA_TEMPLATE_DISPLAY_NAME = "Jev Incident Triage"
METADATA_FIELD_KEYS = {
    "incident_type",
    "triage_decision",
    "severity_score",
    "severity_confidence",
    "escalation_probability",
    "incident_type_confidence",
}
DESTINATION_FOLDER_ENV = {
    "ESCALATE": "BOX_ESCALATE_FOLDER_ID",
    "MONITOR": "BOX_MONITOR_FOLDER_ID",
    "REVIEW": "BOX_REVIEW_FOLDER_ID",
}

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


def metadata_template_key() -> str:
    value = os.environ.get("BOX_METADATA_TEMPLATE_KEY", DEFAULT_METADATA_TEMPLATE_KEY).strip()
    return value or DEFAULT_METADATA_TEMPLATE_KEY


def metadata_template_fields() -> list[CreateMetadataTemplateFields]:
    enum_type = CreateMetadataTemplateFieldsTypeField.ENUM
    return [
        CreateMetadataTemplateFields(
            type=enum_type,
            key="incident_type",
            display_name="Incident type",
            options=[
                CreateMetadataTemplateFieldsOptionsField(key=value)
                for value in ("availability", "security_or_privacy", "data_integrity", "other")
            ],
        ),
        CreateMetadataTemplateFields(
            type=enum_type,
            key="triage_decision",
            display_name="Triage decision",
            options=[
                CreateMetadataTemplateFieldsOptionsField(key=value)
                for value in ("ESCALATE", "REVIEW", "MONITOR")
            ],
        ),
        CreateMetadataTemplateFields(
            type=CreateMetadataTemplateFieldsTypeField.FLOAT,
            key="severity_score",
            display_name="Severity score",
        ),
        CreateMetadataTemplateFields(
            type=CreateMetadataTemplateFieldsTypeField.FLOAT,
            key="severity_confidence",
            display_name="Severity confidence",
        ),
        CreateMetadataTemplateFields(
            type=CreateMetadataTemplateFieldsTypeField.FLOAT,
            key="escalation_probability",
            display_name="Escalation probability",
        ),
        CreateMetadataTemplateFields(
            type=CreateMetadataTemplateFieldsTypeField.FLOAT,
            key="incident_type_confidence",
            display_name="Incident type confidence",
        ),
    ]


def find_metadata_template(client: BoxClient, template_key: str) -> object | None:
    marker = None
    while True:
        page = client.metadata_templates.get_enterprise_metadata_templates(
            marker=marker,
            limit=100,
        )
        for template in page.entries or []:
            if template.template_key == template_key:
                return template
        marker = page.next_marker
        if not marker:
            return None


def ensure_metadata_template(client: BoxClient) -> object:
    """Find the demo template or create it during the one-time setup."""
    template_key = metadata_template_key()
    existing = find_metadata_template(client, template_key)
    if existing:
        return existing

    try:
        return client.metadata_templates.create_metadata_template(
            scope="enterprise",
            display_name=METADATA_TEMPLATE_DISPLAY_NAME,
            template_key=template_key,
            fields=metadata_template_fields(),
        )
    except BoxAPIError as error:
        if error.response_info.status_code == 403:
            raise RuntimeError(
                "Box refused metadata-template creation. Use an Admin or Co-admin "
                "developer token with permission to create and edit metadata templates."
            ) from error
        raise


def validate_metadata_template(template: object) -> str:
    template_key = getattr(template, "template_key", None) or metadata_template_key()
    field_keys = {
        field.key
        for field in (getattr(template, "fields", None) or [])
        if getattr(field, "key", None)
    }
    missing = METADATA_FIELD_KEYS - field_keys
    if missing:
        missing_fields = ", ".join(sorted(missing))
        raise RuntimeError(
            f"Metadata template {template_key!r} is missing fields: {missing_fields}. "
            "Choose a new BOX_METADATA_TEMPLATE_KEY or recreate the demo template."
        )
    return template_key


def setup_metadata_template(client: BoxClient) -> None:
    template = ensure_metadata_template(client)
    template_key = validate_metadata_template(template)
    print(
        f"Ready to use Box metadata template {template_key!r} "
        f"({getattr(template, 'scope', 'enterprise')})."
    )


def metadata_values(response: object, decision: str) -> dict[str, object]:
    answers = response.answers
    incident_type = answers["incident_type"]
    severity = answers["severity"]
    escalation = answers["needs_escalation"]
    return {
        "incident_type": incident_type.choice,
        "triage_decision": decision,
        "severity_score": round(float(severity.score), 4),
        "severity_confidence": round(float(severity.confidence), 4),
        "escalation_probability": round(float(escalation.noul), 4),
        "incident_type_confidence": round(float(incident_type.confidence), 4),
    }


def apply_metadata(
    client: BoxClient,
    file_id: str,
    template: object,
    response: object,
    decision: str,
) -> str:
    """Create or update the Jev metadata instance on the source file."""
    template_key = validate_metadata_template(template)
    values = metadata_values(response, decision)
    try:
        client.file_metadata.get_file_metadata_by_id(
            file_id,
            GetFileMetadataByIdScope.ENTERPRISE,
            template_key,
        )
    except BoxAPIError as error:
        if error.response_info.status_code != 404:
            raise
        client.file_metadata.create_file_metadata_by_id(
            file_id,
            CreateFileMetadataByIdScope.ENTERPRISE,
            template_key,
            values,
        )
        return "created"

    updates = [
        UpdateFileMetadataByIdRequestBody(
            op=UpdateFileMetadataByIdRequestBodyOpField.REPLACE,
            path=f"/{key}",
            value=value,
        )
        for key, value in values.items()
    ]
    client.file_metadata.update_file_metadata_by_id(
        file_id,
        UpdateFileMetadataByIdScope.ENTERPRISE,
        template_key,
        updates,
    )
    return "updated"


def get_file_name(client: BoxClient, file_id: str) -> str:
    file = client.files.get_file_by_id(file_id, fields=["id", "name"])
    if not file.name:
        raise RuntimeError(f"Box returned no name for file {file_id}.")
    return file.name


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


def move_report(client: BoxClient, file_id: str, source_folder_id: str, decision: str) -> str:
    destination_env = DESTINATION_FOLDER_ENV[decision]
    destination_folder_id = required(destination_env)
    if destination_folder_id == source_folder_id:
        raise RuntimeError(
            f"{destination_env} must be different from BOX_FOLDER_ID so the report can be routed."
        )
    client.files.update_file_by_id(
        file_id,
        parent=UpdateFileByIdParent(id=destination_folder_id),
    )
    return destination_folder_id


def create_review_task(client: BoxClient, file_id: str, file_name: str, decision: str) -> str | None:
    """Create and assign a Box review task for escalated outcomes."""
    if decision != "ESCALATE":
        return None

    assignee_id = required("BOX_REVIEW_ASSIGNEE_ID")
    message = os.environ.get("BOX_REVIEW_TASK_MESSAGE", "").strip()
    if not message:
        message = (
            f"Please review the escalated incident triage for {file_name} "
            "and confirm the next action."
        )

    task = client.tasks.create_task(
        CreateTaskItem(id=file_id, type=CreateTaskItemTypeField.FILE),
        action=CreateTaskAction.REVIEW,
        message=message,
        completion_rule=CreateTaskCompletionRule.ANY_ASSIGNEE,
    )
    if not task.id:
        raise RuntimeError("Box created a task without returning a task ID.")

    assignment = client.task_assignments.create_task_assignment(
        CreateTaskAssignmentTask(id=task.id),
        CreateTaskAssignmentAssignTo(id=assignee_id),
    )
    if not assignment.id:
        raise RuntimeError("Box created a task assignment without returning an assignment ID.")
    return task.id


def run(args: argparse.Namespace) -> None:
    if args.create_review_task and not args.write_back:
        raise RuntimeError("--create-review-task requires --write-back.")
    if args.setup_template:
        if args.local:
            raise RuntimeError("--setup-template requires a Box file flow, not --local.")
        setup_metadata_template(box_client())
        return

    file_id = None
    if args.local:
        file_name = Path(args.local).name
        source_text = Path(args.local).read_text(encoding="utf-8")
        client = None
        folder_id = ""
    else:
        client = box_client()
        folder_id = required("BOX_FOLDER_ID")
        file_id = required("BOX_FILE_ID")
        file_name = get_file_name(client, file_id)
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
        if client is None or file_id is None:
            raise RuntimeError("--write-back requires a Box source document.")
        template = find_metadata_template(client, metadata_template_key())
        if template is None:
            raise RuntimeError(
                "The Jev Incident Triage metadata template was not found. "
                "Run `python incident_triage.py --setup-template` once first."
            )
        metadata_action = apply_metadata(client, file_id, template, response, decision)
        print(f"Box metadata instance {metadata_action} on {file_name}")
        output_name = write_back(client, folder_id, file_name, card)
        print(f"Saved decision card to Box as {output_name}")
        destination_folder_id = move_report(client, file_id, folder_id, decision)
        print(f"Moved {file_name} to {decision} folder {destination_folder_id}")
        if args.create_review_task:
            task_id = create_review_task(client, file_id, file_name, decision)
            if task_id:
                print(f"Created Box review task {task_id} for {decision.lower()} outcome")
            else:
                print("No review task created for MONITOR outcome")


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
        help="Apply metadata, upload the decision card, and route the source PDF.",
    )
    parser.add_argument(
        "--setup-template",
        action="store_true",
        help="Create or reuse the enterprise metadata template, then exit.",
    )
    parser.add_argument(
        "--create-review-task",
        action="store_true",
        help="After routing, create and assign a Box review task for ESCALATE only.",
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
