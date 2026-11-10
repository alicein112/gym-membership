import re
import threading
import uuid
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Optional

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse

COUNTRIES = {
    "AE": {"dial": "971", "currency": "AED", "vat": 5},
    "SA": {"dial": "966", "currency": "SAR", "vat": 15},
    "BH": {"dial": "973", "currency": "BHD", "vat": 10},
}
VERDICTS = (
    "MATCHED",
    "DUE_DAY_DRIFT",
    "DOUBLE_CHARGE_RISK",
    "MISSING_FROM_LEDGER",
    "DECLINED",
)

app = FastAPI()
state_lock = threading.RLock()
members_by_id: dict[str, dict[str, str]] = {}
members_by_phone: dict[str, str] = {}
plans_by_id: dict[str, dict[str, Any]] = {}
agreements_by_id: dict[str, dict[str, Any]] = {}
runs_by_id: dict[str, dict[str, Any]] = {}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        self.status = status
        self.payload = {"error": {"code": code, "message": message, **extra}}


def fail(status: int, code: str, message: str, **extra: Any) -> None:
    raise ApiError(status, code, message, **extra)


def new_id() -> str:
    while True:
        value = str(uuid.uuid4())
        if (
            value not in members_by_id
            and value not in plans_by_id
            and value not in agreements_by_id
            and value not in runs_by_id
        ):
            return value


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.payload)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "VALIDATION", "message": "Request validation failed."}},
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
    message = "Resource was not found." if exc.status_code == 404 else "HTTP request failed."
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": message}},
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "An internal error occurred."}},
    )


async def read_json_object(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception:
        fail(422, "VALIDATION", "Request body must be valid JSON.")
    if not isinstance(value, dict):
        fail(422, "VALIDATION", "Request body must be a JSON object.")
    return value


def required_string(data: dict[str, Any], key: str, *, trim: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        fail(422, "VALIDATION", f"{key} must be a string.")
    if trim:
        value = value.strip()
    if not value:
        fail(422, "VALIDATION", f"{key} must not be empty.")
    return value


def required_integer(data: dict[str, Any], key: str, minimum: Optional[int] = None) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        fail(422, "VALIDATION", f"{key} must be an integer.")
    if minimum is not None and value < minimum:
        fail(422, "VALIDATION", f"{key} must be at least {minimum}.")
    return value


def required_country(data: dict[str, Any], key: str = "country") -> str:
    value = required_string(data, key)
    if value not in COUNTRIES:
        fail(422, "VALIDATION", f"{key} must be AE, SA or BH.")
    return value


def parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        fail(422, "VALIDATION", f"{field} must be a date in YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail(422, "VALIDATION", f"{field} must be a valid date.")


def parse_month(value: Any) -> tuple[int, int, date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        fail(422, "VALIDATION", "month must be in YYYY-MM format.")
    year, month = int(value[:4]), int(value[5:7])
    if month < 1 or month > 12:
        fail(422, "VALIDATION", "month must be a valid calendar month.")
    try:
        first = date(year, month, 1)
        last = date(year, month, monthrange(year, month)[1])
    except ValueError:
        fail(422, "VALIDATION", "month must be a valid calendar month.")
    return year, month, first, last


def national_matches(value: str, country: str) -> bool:
    if country in ("AE", "SA"):
        return re.fullmatch(r"05\d{8}", value) is not None
    return re.fullmatch(r"3\d{7}", value) is not None


def international_matches(value: str, country: str) -> bool:
    if country in ("AE", "SA"):
        return re.fullmatch(r"5\d{8}", value) is not None
    return re.fullmatch(r"3\d{7}", value) is not None


def canonical_phone(raw: str, *, member_country: Optional[str] = None, lookup_country: Optional[str] = None) -> str:
    value = raw.translate(str.maketrans("", "", " -()"))
    if not value or re.fullmatch(r"\+?\d+", value) is None or value.count("+") > 1:
        fail(422, "INVALID_PHONE", "Phone number is invalid.")
    if value.startswith("+"):
        digits = value[1:]
        has_plus = True
    else:
        digits = value
        has_plus = False
    if digits.startswith("00"):
        fail(422, "INVALID_PHONE", "Phone number is invalid.")
    international_country = None
    international_mobile = None
    for country, config in COUNTRIES.items():
        dial = config["dial"]
        if digits.startswith(dial):
            suffix = digits[len(dial):]
            if international_matches(suffix, country):
                international_country = country
                international_mobile = suffix
                break
    if international_country is not None:
        if lookup_country is not None and lookup_country != international_country:
            fail(422, "PHONE_COUNTRY_MISMATCH", "Phone country does not match the supplied country.")
        return f"+{COUNTRIES[international_country]['dial']}{international_mobile}"
    if has_plus:
        fail(422, "INVALID_PHONE", "Phone number is invalid.")
    if member_country is not None:
        if not national_matches(digits, member_country):
            fail(422, "INVALID_PHONE", "Phone number is invalid.")
        country = member_country
    elif lookup_country is not None:
        if not national_matches(digits, lookup_country):
            fail(422, "INVALID_PHONE", "Phone number is invalid.")
        country = lookup_country
    else:
        matches = [country for country in COUNTRIES if national_matches(digits, country)]
        if matches:
            fail(422, "PHONE_NEEDS_COUNTRY", "A country is required for a national phone number.")
        fail(422, "INVALID_PHONE", "Phone number is invalid.")
    if country in ("AE", "SA"):
        mobile = digits[1:]
    else:
        mobile = digits
    return f"+{COUNTRIES[country]['dial']}{mobile}"


def month_add(year: int, month: int, delta: int) -> tuple[int, int]:
    zero_based = year * 12 + month - 1 + delta
    return zero_based // 12, zero_based % 12 + 1


def due_date(year: int, month: int, anchor_day: int) -> date:
    return date(year, month, min(anchor_day, monthrange(year, month)[1]))


def first_monthly_due(start: date, anchor_day: int) -> date:
    this_month = due_date(start.year, start.month, anchor_day)
    if this_month > start:
        return this_month
    year, month = month_add(start.year, start.month, 1)
    return due_date(year, month, anchor_day)


def round_half_up(numerator: int, denominator: int) -> int:
    return (2 * numerator + denominator) // (2 * denominator)


def invoice_amounts(plan: dict[str, Any], net: int) -> tuple[int, int, int]:
    vat = round_half_up(net * plan["vat"], 100)
    return net, vat, net + vat


def first_invoice(agreement: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    start = agreement["start_date"]
    next_due = first_monthly_due(start, agreement["due_day"])
    previous_year, previous_month = month_add(next_due.year, next_due.month, -1)
    previous_due = due_date(previous_year, previous_month, agreement["due_day"])
    days = (next_due - start).days
    cycle_days = (next_due - previous_due).days
    prorated = round_half_up(plan["monthly"] * days, cycle_days)
    net, vat, total = invoice_amounts(plan, prorated + plan["joining_fee"])
    return {
        "invoice_no": 1,
        "due_date": start.isoformat(),
        "kind": "FIRST",
        "proration": {"days": days, "cycle_days": cycle_days},
        "net": net,
        "vat": vat,
        "total": total,
        "currency": plan["currency"],
    }


def invoices_for_range(agreement: dict[str, Any], plan: dict[str, Any], start_date: date, end_date: date) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    start = agreement["start_date"]
    if start_date <= start <= end_date:
        result.append(first_invoice(agreement, plan))
    first_due = first_monthly_due(start, agreement["due_day"])
    first_year_month = first_due.year * 12 + first_due.month - 1
    requested_year_month = start_date.year * 12 + start_date.month - 1
    index = max(0, requested_year_month - first_year_month)
    year, month = month_add(first_due.year, first_due.month, index)
    current_due = due_date(year, month, agreement["due_day"])
    if current_due < start_date:
        index += 1
        year, month = month_add(first_due.year, first_due.month, index)
        current_due = due_date(year, month, agreement["due_day"])
    while current_due <= end_date:
        net, vat, total = invoice_amounts(plan, plan["monthly"])
        result.append({
            "invoice_no": index + 2,
            "due_date": current_due.isoformat(),
            "kind": "MONTHLY",
            "proration": None,
            "net": net,
            "vat": vat,
            "total": total,
            "currency": plan["currency"],
        })
        index += 1
        year, month = month_add(first_due.year, first_due.month, index)
        if year > 9999:
            break
        current_due = due_date(year, month, agreement["due_day"])
    return sorted(result, key=lambda item: (item["due_date"], item["invoice_no"]))


def agreement_invoice_key(agreement_id: str, invoice_no: int) -> tuple[str, int]:
    return agreement_id, invoice_no


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/members", status_code=201)
async def create_member(request: Request) -> JSONResponse:
    data = await read_json_object(request)
    name = required_string(data, "name", trim=True)
    if not 1 <= len(name) <= 120:
        fail(422, "VALIDATION", "name must contain between 1 and 120 characters.")
    country = required_country(data)
    phone = data.get("phone")
    if not isinstance(phone, str):
        fail(422, "VALIDATION", "phone must be a string.")
    normalized = canonical_phone(phone, member_country=country)
    with state_lock:
        existing_id = members_by_phone.get(normalized)
        if existing_id is not None:
            fail(409, "PHONE_TAKEN", "A member already has this phone number.", existing_member_id=existing_id)
        member_id = new_id()
        member = {"id": member_id, "name": name, "country": country, "phone": normalized}
        members_by_id[member_id] = member
        members_by_phone[normalized] = member_id
    return JSONResponse(status_code=201, content=member)


@app.get("/members/lookup")
async def lookup_member(phone: Optional[str] = None, country: Optional[str] = None) -> dict[str, str]:
    if phone is None:
        fail(422, "VALIDATION", "phone is required.")
    if country is not None and country not in COUNTRIES:
        fail(422, "VALIDATION", "country must be AE, SA or BH.")
    normalized = canonical_phone(phone, lookup_country=country)
    with state_lock:
        member_id = members_by_phone.get(normalized)
        if member_id is None:
            fail(404, "NOT_FOUND", "Member was not found.")
        return dict(members_by_id[member_id])


@app.post("/plans", status_code=201)
async def create_plan(request: Request) -> JSONResponse:
    data = await read_json_object(request)
    plan_id = required_string(data, "id")
    country = required_country(data)
    monthly = required_integer(data, "monthly", 1)
    joining_fee = required_integer(data, "joining_fee", 0)
    config = COUNTRIES[country]
    plan = {
        "id": plan_id,
        "country": country,
        "currency": config["currency"],
        "monthly": monthly,
        "joining_fee": joining_fee,
        "vat": config["vat"],
    }
    with state_lock:
        if plan_id in plans_by_id:
            fail(409, "PLAN_EXISTS", "A plan with this id already exists.")
        plans_by_id[plan_id] = plan
    return JSONResponse(status_code=201, content={key: value for key, value in plan.items() if key != "vat"})


@app.post("/agreements", status_code=201)
async def create_agreement(request: Request) -> JSONResponse:
    data = await read_json_object(request)
    member_id = required_string(data, "member_id")
    plan_id = required_string(data, "plan_id")
    start = parse_date(data.get("start_date"), "start_date")
    due_day = required_integer(data, "due_day", 1)
    if due_day > 31:
        fail(422, "VALIDATION", "due_day must be between 1 and 31.")
    with state_lock:
        member = members_by_id.get(member_id)
        if member is None:
            fail(422, "UNKNOWN_MEMBER", "Member does not exist.")
        plan = plans_by_id.get(plan_id)
        if plan is None:
            fail(422, "UNKNOWN_PLAN", "Plan does not exist.")
        if plan["country"] != member["country"]:
            fail(422, "PLAN_COUNTRY_MISMATCH", "Plan country does not match member country.")
        agreement_id = new_id()
        agreement = {
            "id": agreement_id,
            "member_id": member_id,
            "plan_id": plan_id,
            "start_date": start,
            "due_day": due_day,
        }
        agreements_by_id[agreement_id] = agreement
    return JSONResponse(status_code=201, content={
        "id": agreement_id,
        "member_id": member_id,
        "plan_id": plan_id,
        "start_date": start.isoformat(),
        "due_day": due_day,
    })


@app.get("/agreements/{agreement_id}/invoices")
async def get_invoices(
    agreement_id: str,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
) -> list[dict[str, Any]]:
    if from_date is None or to_date is None:
        fail(422, "VALIDATION", "from and to are required.")
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    if end < start or (end - start).days > 366:
        fail(422, "INVALID_RANGE", "Date range must be ordered and no more than 366 days.")
    with state_lock:
        agreement = agreements_by_id.get(agreement_id)
        if agreement is None:
            fail(404, "NOT_FOUND", "Agreement was not found.")
        plan = plans_by_id[agreement["plan_id"]]
        invoices = invoices_for_range(agreement, plan, start, end)
    return invoices


@app.post("/reconciliation/runs", status_code=201)
async def create_reconciliation_run(request: Request) -> JSONResponse:
    data = await read_json_object(request)
    month_value = data.get("month")
    year, month, month_start, month_end = parse_month(month_value)
    rows_value = data.get("rows")
    if not isinstance(rows_value, list):
        fail(422, "VALIDATION", "rows must be an array.")
    parsed_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows_value):
        if not isinstance(row, dict):
            fail(422, "VALIDATION", f"rows[{index}] must be an object.")
        gateway_txn_id = required_string(row, "gateway_txn_id")
        agreement_id = required_string(row, "agreement_id")
        amount = required_integer(row, "amount", 1)
        currency = required_string(row, "currency")
        status = required_string(row, "status")
        if status not in ("SETTLED", "DECLINED"):
            fail(422, "VALIDATION", f"rows[{index}].status must be SETTLED or DECLINED.")
        attempted = parse_date(row.get("attempted_on"), f"rows[{index}].attempted_on")
        parsed_rows.append({
            "gateway_txn_id": gateway_txn_id,
            "agreement_id": agreement_id,
            "amount": amount,
            "currency": currency,
            "status": status,
            "attempted_on": attempted,
        })
    errors: list[dict[str, str]] = []
    txn_counts = Counter(row["gateway_txn_id"] for row in parsed_rows)
    with state_lock:
        for row in parsed_rows:
            txn_id = row["gateway_txn_id"]
            if txn_counts[txn_id] > 1:
                errors.append({"gateway_txn_id": txn_id, "code": "DUPLICATE_TXN"})
            if row["attempted_on"].year != year or row["attempted_on"].month != month:
                errors.append({"gateway_txn_id": txn_id, "code": "ROW_OUTSIDE_MONTH"})
            agreement = agreements_by_id.get(row["agreement_id"])
            if agreement is not None:
                plan = plans_by_id[agreement["plan_id"]]
                if plan["currency"] != row["currency"]:
                    errors.append({"gateway_txn_id": txn_id, "code": "CURRENCY_MISMATCH"})
        if errors:
            fail(422, "INVALID_ROWS", "One or more gateway rows are invalid.", errors=errors)
        expected: dict[tuple[str, int], dict[str, Any]] = {}
        expected_by_agreement: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for agreement_id, agreement in agreements_by_id.items():
            plan = plans_by_id[agreement["plan_id"]]
            for invoice in invoices_for_range(agreement, plan, month_start, month_end):
                record = {
                    "agreement_id": agreement_id,
                    "invoice_no": invoice["invoice_no"],
                    "due_date": invoice["due_date"],
                    "total": invoice["total"],
                    "currency": invoice["currency"],
                }
                key = agreement_invoice_key(agreement_id, invoice["invoice_no"])
                expected[key] = record
                expected_by_agreement[agreement_id].append(record)
        assignments: list[Optional[tuple[str, int]]] = []
        linked_counts: Counter[tuple[str, int]] = Counter()
        for row in parsed_rows:
            if row["status"] == "DECLINED":
                assignments.append(None)
                continue
            candidates = [
                invoice
                for invoice in expected_by_agreement.get(row["agreement_id"], [])
                if abs(invoice["total"] - row["amount"]) <= 1
            ]
            if not candidates:
                assignments.append(None)
                continue
            candidate = min(candidates, key=lambda item: (item["due_date"], item["invoice_no"]))
            key = agreement_invoice_key(candidate["agreement_id"], candidate["invoice_no"])
            assignments.append(key)
            linked_counts[key] += 1
        output_rows: list[dict[str, Any]] = []
        for row, assignment in zip(parsed_rows, assignments):
            if row["status"] == "DECLINED":
                verdict = "DECLINED"
            elif assignment is None:
                verdict = "MISSING_FROM_LEDGER"
            elif linked_counts[assignment] >= 2:
                verdict = "DOUBLE_CHARGE_RISK"
            else:
                invoice_record = expected[assignment]
                verdict = "MATCHED" if row["attempted_on"].isoformat() == invoice_record["due_date"] else "DUE_DAY_DRIFT"
            output_rows.append({
                "gateway_txn_id": row["gateway_txn_id"],
                "verdict": verdict,
                "invoice": None if assignment is None else {
                    "agreement_id": assignment[0],
                    "invoice_no": assignment[1],
                },
            })
        summary = {verdict: 0 for verdict in VERDICTS}
        for row in output_rows:
            summary[row["verdict"]] += 1
        unmatched = [
            invoice
            for key, invoice in expected.items()
            if linked_counts[key] == 0
        ]
        unmatched.sort(key=lambda item: (item["due_date"], item["agreement_id"], item["invoice_no"]))
        run_id = new_id()
        body = {
            "id": run_id,
            "month": month_value,
            "summary": summary,
            "rows": output_rows,
            "unmatched_invoices": unmatched,
        }
        runs_by_id[run_id] = body
    return JSONResponse(status_code=201, content=body)


@app.get("/reconciliation/runs/{run_id}")
async def get_reconciliation_run(run_id: str) -> dict[str, Any]:
    with state_lock:
        run = runs_by_id.get(run_id)
        if run is None:
            fail(404, "NOT_FOUND", "Reconciliation run was not found.")
        return run
