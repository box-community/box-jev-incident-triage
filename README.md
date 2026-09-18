# Documents can make decisions

This is a small Box + Jev demo.

An incident report arrives in Box as a complex PDF. The demo asks Box for its
Markdown representation with the Box Python SDK, asks Jev three narrow
questions, and lets a short piece of ordinary Python decide whether the incident
should be monitored, reviewed, or escalated.

The point is not to make Jev write a better summary.

The point is to make the document useful to a workflow and write the resulting
signals back onto the source file as Box metadata.

```text
Box PDF → Box Markdown representation → Jev judgments → application policy → Box metadata + next action
```

Jev supplies the semantic signals. Code owns the policy. A person remains in the
loop for a consequential decision.

## What Jev answers

The demo uses one System One request with three typed questions:

- `Choice`: what kind of incident is this?
- `Score`: how severe is it?
- `Noul`: does it require immediate escalation?

The policy is deliberately visible in `incident_triage.py`:

```python
if escalation_probability >= 0.75:
    return "ESCALATE"
if severity.score >= 1.0 or low_confidence:
    return "REVIEW"
return "MONITOR"
```

That boundary is the story. Jev does not take an opaque prompt and return an
opaque paragraph. It returns small, typed judgments that an application can
combine, inspect, test, and route.

## Run it

Create a Box app with a developer token, create a TypeSafe API key, and install
the two SDKs:

```bash
cd jev-incident-triage
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Upload `sample-document/incident-report.pdf` to the Box folder configured in
`.env`, then set:

```dotenv
BOX_DEVELOPER_TOKEN=your_box_developer_token
BOX_FOLDER_ID=your_box_folder_id
BOX_FILE_NAME=incident-report.pdf
BOX_METADATA_TEMPLATE_KEY=jevIncidentTriage
BOX_ESCALATE_FOLDER_ID=your_escalate_folder_id
BOX_MONITOR_FOLDER_ID=your_monitor_folder_id
BOX_REVIEW_FOLDER_ID=your_review_folder_id
TYPESAFE_API_KEY=your_typesafe_api_key
```

Run this one-time setup command to create the enterprise metadata template:

```bash
python incident_triage.py --setup-template
```

The setup is idempotent: if the template key already exists, the script reuses
it. Creating an enterprise metadata template may require an Admin or Co-admin
developer token with permission to create and edit metadata templates.

Run the live Box flow:

```bash
python incident_triage.py --write-back
```

The `--write-back` flag applies the extracted incident type, severity score,
confidence values, escalation probability, and policy decision to the source PDF
as Box metadata. It uploads a timestamped Markdown decision card to the intake
folder, then moves the source PDF to the folder selected by the policy:

```text
ESCALATE → BOX_ESCALATE_FOLDER_ID
REVIEW    → BOX_REVIEW_FOLDER_ID
MONITOR   → BOX_MONITOR_FOLDER_ID
```

If the metadata instance already exists, repeated runs update it in place. Use a
new copy of the sample PDF in the intake folder for each end-to-end run, because
the classified source file is moved out of that folder.

To rehearse the Jev part without Box credentials, use the checked-in Markdown
fixture. In the live flow, Box creates this representation from the PDF. The
first request may take a few seconds while Box generates the representation; the
script checks its status and waits for it to become ready:

```bash
python incident_triage.py --local sample-document/incident-report.md
```

Box supports Markdown representations for PDF files. The representation keeps
headings, tables, lists, and other formatting cues, so Jev receives the shape of
the incident packet rather than a lossy paragraph dump. See the [Box Markdown
representation guide](https://developer.box.com/guides/representations/markdown).

## The article angle

Most document AI demos stop at extraction or summarization. Those are useful,
but they leave the document at the edge of the system. The more interesting
version starts with a document that is genuinely difficult to reduce to a few
fields: a multi-page incident packet with a timeline, conflicting reports,
partial evidence, and open questions.

The more interesting question is: **what can the rest of the application do once
the meaning of the document is typed?**

An incident report is a good example because the desired output is not a prose
summary. Someone needs to know what happens next. Is this routine? Does it need
another team? Is there a reason to escalate now?

The document does not become a source of truth just because a model read it. It
becomes part of a decision mechanism when its meaning is converted into signals
that software can use. Here, the PDF is preserved as the human-readable source,
while Box supplies a structured Markdown representation for the decision step.

That is the shift this demo makes visible:

```text
unstructured content → structured judgment → controlled action
```

The same pattern can sit behind many workflows. The domain changes. The shape of
the integration does not.

## Guardrails

This is an incident triage signal, not a security certification, legal opinion,
or automatic customer-impact determination. Thresholds are demo values and need
to be evaluated against representative incidents before being used in a real
workflow. Keep TypeSafe and Box credentials on the server side.
