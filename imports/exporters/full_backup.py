import io
import json
import os
import zipfile

from .json_export import export_items_json

ITEMS_JSON_NAME = 'items.json'
FILES_DIR = 'files'
DOCUMENTS_DIR = 'documents'


def export_full_backup(items) -> bytes:
    """
    A "Full Backup" bundle: items.json (same shape as the plain JSON export,
    plus an internal _id/_file/_documents index) plus the actual item.file
    and Document attachments, so restoring it doesn't lose anything the
    plain CSV/JSON export can't carry.
    """
    entries = export_items_json(items)
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for entry, item in zip(entries, items):
            entry['_id'] = str(item.id)

            if item.file:
                arcname = f'{FILES_DIR}/{item.id}_{os.path.basename(item.file.name)}'
                zf.writestr(arcname, item.file.read())
                entry['_file'] = arcname

            doc_names = []
            for document in item.documents.all():
                arcname = f'{DOCUMENTS_DIR}/{item.id}/{document.id}_{os.path.basename(document.file.name)}'
                zf.writestr(arcname, document.file.read())
                doc_names.append(arcname)
            if doc_names:
                entry['_documents'] = doc_names

        zf.writestr(ITEMS_JSON_NAME, json.dumps(entries, indent=2))

    return buffer.getvalue()
