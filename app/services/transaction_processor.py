from collections import Counter
from decimal import Decimal
from app.core.config import settings
from app.services.field_detector import detect_fields, looks_like_transaction
from app.services.amount_normalizer import parse_amount
from app.services.currency_normalizer import normalize_currency
from app.services.date_normalizer import normalize_date
from app.services.status_normalizer import normalize_status
from app.models.transaction import NormalizedTransaction


def _selected(found, field):
    candidates = found.get(field, [])
    return candidates[0] if candidates else None

def _different(candidates):
    values = []
    for item in candidates:
        value = item["value"]
        if isinstance(value, (dict, list)):
            value = repr(value)
        values.append(str(value).strip())
    return len(set(values)) > 1

def _warn_conflicts(found, warnings):
    for field, candidates in found.items():
        if len(candidates) > 1 and _different(candidates):
            chosen = candidates[0]
            warnings.append({"code": "FIELD_CONFLICT", "field": field,
                "message": f"Se encontraron propiedades candidatas contradictorias para {field}; prevalece {chosen['path']}.",
                "candidates": [{"path": c["path"], "value": c["value"]} for c in candidates]})

def process_transaction(record, position, normalized_id, timezone_name=None, date_order=None):
    found = detect_fields(record)
    warnings = []
    _warn_conflicts(found, warnings)
    trace = {}
    for field in ("amount", "currency", "transactionDate", "status", "originalReference", "concept"):
        candidate = _selected(found, field)
        trace[field] = ({"path": candidate["path"], "sourceKey": candidate["key"],
                         "originalValue": candidate["value"], "method": candidate["method"]}
                        if candidate else None)

    amount_candidate = _selected(found, "amount")
    if not amount_candidate:
        return None, {"position": position, "original": record, "errors": ["No se encontró una propiedad válida de monto."], "warnings": warnings, "trace": trace}
    try:
        amount, embedded_iso, symbol_currency = parse_amount(amount_candidate["value"])
    except ValueError as exc:
        return None, {"position": position, "original": record, "errors": [str(exc)], "warnings": warnings, "trace": trace}

    currency_candidate = _selected(found, "currency")
    currency_value = currency_candidate["value"] if currency_candidate else None
    currency, currency_warnings = normalize_currency(currency_value, embedded_iso, symbol_currency)
    for message in currency_warnings:
        warnings.append({"code": "CURRENCY_WARNING", "field": "currency", "message": message})

    date_candidate = _selected(found, "transactionDate")
    date_value = date_candidate["value"] if date_candidate else None
    transaction_date, date_warning = normalize_date(date_value, timezone_name, date_order)
    if date_warning:
        warnings.append({"code": "DATE_WARNING", "field": "transactionDate", "message": date_warning})

    status_candidate = _selected(found, "status")
    status = normalize_status(status_candidate["value"] if status_candidate else None)
    ref_candidate = _selected(found, "originalReference")
    reference = ref_candidate["value"] if ref_candidate else None
    if reference is not None:
        if isinstance(reference, (dict, list)):
            reference = str(reference)
        else:
            reference = str(reference)
    concept_candidate = _selected(found, "concept")
    concept = concept_candidate["value"] if concept_candidate else None
    if concept is not None and not isinstance(concept, str):
        concept = str(concept)

    result = NormalizedTransaction(
        id=normalized_id, amount=amount, currency=currency,
        transactionDate=transaction_date, status=status,
        originalReference=reference, concept=concept,
    ).model_dump()
    return result, {"position": position, "warnings": warnings, "trace": trace}


def _discover_records(payload):
    """Return candidate transaction items, preserving document order and avoiding nested duplicate arrays."""
    found = []
    def visit(node, path=(), depth=0):
        if depth > settings.max_depth:
            return
        if isinstance(node, list):
            dict_items = [x for x in node if isinstance(x, dict)]
            if dict_items and any(looks_like_transaction(x) for x in dict_items):
                for idx, item in enumerate(node):
                    found.append((item, ".".join(map(str, path + (idx,))) or str(idx)))
                return
            for idx, item in enumerate(node):
                visit(item, path + (idx,), depth + 1)
        elif isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    visit(value, path + (key,), depth + 1)
    # Search transaction arrays first so wrapper metadata (for example a top-level id)
    # is not mistaken for a transaction when a nested transaction collection exists.
    visit(payload)
    if found:
        return found
    if isinstance(payload, dict):
        direct = {"amount", "currency", "transactionDate", "status", "originalReference", "concept"}
        direct_fields = set()
        for key in payload:
            from app.services.field_detector import normalize_key
            nk = normalize_key(key)
            for field, aliases in __import__("app.core.constants", fromlist=["FIELD_ALIASES"]).FIELD_ALIASES.items():
                if nk in aliases:
                    direct_fields.add(field)
        if direct_fields:
            return [(payload, "root")]
    return found


def process_payload(payload, timezone_name=None, date_order=None):
    records = _discover_records(payload)
    if not records:
        raise ValueError("No se encontraron transacciones en la estructura JSON proporcionada.")
    if len(records) > settings.max_transactions:
        raise ValueError(f"El documento supera el máximo permitido de {settings.max_transactions} elementos.")
    valid, invalid, details = [], [], []
    warning_count = 0
    for position, (record, source_path) in enumerate(records, start=1):
        if not isinstance(record, dict):
            invalid_item = {"position": position, "sourcePath": source_path, "original": record,
                            "errors": ["El elemento no es un objeto JSON."], "warnings": [], "trace": {}}
            invalid.append(invalid_item)
            continue
        normalized, detail = process_transaction(record, position, len(valid) + 1, timezone_name, date_order)
        detail["sourcePath"] = source_path
        if normalized is None:
            invalid.append(detail)
        else:
            valid.append(normalized)
            details.append({"id": normalized["id"], "original": record, **detail})
            warning_count += bool(detail["warnings"])
    status_counts = dict(Counter(x["status"] for x in valid))
    currency_counts = dict(Counter(x["currency"] or "UNKNOWN" for x in valid))
    dates_unavailable = sum(1 for x in valid if x["transactionDate"] is None)
    unknown_currency = sum(1 for x in valid if x["currency"] is None)
    total = len(records)
    valid_count, invalid_count = len(valid), len(invalid)
    metrics = {
        "processed": total, "valid": valid_count, "invalid": invalid_count,
        "validPercentage": round(valid_count * 100 / total, 2) if total else 0,
        "invalidPercentage": round(invalid_count * 100 / total, 2) if total else 0,
        "withWarnings": int(warning_count), "unknownCurrency": unknown_currency,
        "unavailableDates": dates_unavailable,
        "statusCounts": status_counts, "currencyCounts": currency_counts,
    }
    return {"transactions": valid, "invalidTransactions": invalid, "details": details, "metrics": metrics}
