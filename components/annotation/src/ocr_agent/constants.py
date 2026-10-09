MAX_TITLE_LEVEL = 4

ALLOWED_TYPES = {
    "text",
    "title",
    "table",
    "table_caption",
    "image",
    "image_caption",
    "chart",
    "seal",
    "formula",
    "header",
    "footer",
    "page_number",
    "footnote",
}

TEXT_TYPES = {
    "text",
    "title",
    "table_caption",
    "image_caption",
    "formula",
    "header",
    "footer",
    "page_number",
    "footnote",
}

CAPTION_TYPES = {"table_caption", "image_caption"}
MEDIA_TYPES = {"image", "chart", "seal"}

ALLOWED_OPERATIONS = {
    "add",
    "remove_duplicate",
    "set_type",
    "set_level",
    "split",
    "merge",
    "replace_text",
    "set_bbox",
    "reorder",
    "set_table_html",
    "set_parent",
    "mark_review",
}

SCHEMA_VERSION = "ocr-agent-page-evidence.v4"
REVISION_VERSION = "ocr-agent-revision.v4"
FIELD_REVIEW_VERSION = "ocr-agent-field-review.v2"

FIELD_STATUSES = {"PASS", "CHECK"}
PAGE_STATUSES = {"PASS", "CHECK", "FAIL"}
STATUS_VERSION = "ocr-agent-status.v1"
