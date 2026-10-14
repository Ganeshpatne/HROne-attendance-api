import os
import math
import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv(override=False)

IST = timezone(timedelta(hours=5, minutes=30))

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

def ensure_indexes():
    try:
        db.employees.create_index("emp_code", unique=True)
        db.employees.create_index([("department", 1), ("joined_on", 1)])
        db.employees.create_index([("joined_on", 1), ("department", 1)])
        
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    except Exception:
        pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes idempotently at startup
    ensure_indexes()
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)

def parse_shift_time(time_str: str) -> tuple[int, int]:
    h, m = map(int, time_str.split(":"))
    return h, m

def get_attendance_date_and_shift(punch_in_utc: datetime, shift_start: str, shift_end: str) -> tuple[str, datetime, datetime]:
    """Returns (date_str, shift_start_dt, shift_end_dt) all in IST"""
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh, sm = parse_shift_time(shift_start)
    eh, em = parse_shift_time(shift_end)
    
    is_overnight = (eh, em) <= (sh, sm)
    
    # Base date is punch_in_ist's date
    base_date = punch_in_ist.date()
    start_dt = datetime(base_date.year, base_date.month, base_date.day, sh, sm, tzinfo=IST)
    end_dt = datetime(base_date.year, base_date.month, base_date.day, eh, em, tzinfo=IST)
    
    if is_overnight:
        if punch_in_ist < end_dt:
            base_date -= timedelta(days=1)
            start_dt -= timedelta(days=1)
            end_dt = datetime(base_date.year, base_date.month, base_date.day, eh, em, tzinfo=IST) + timedelta(days=1)
        else:
            end_dt += timedelta(days=1)
            
    return base_date.isoformat(), start_dt, end_dt

def compute_late_minutes(punch_in: datetime, start_dt: datetime) -> int:
    diff = (punch_in - start_dt).total_seconds()
    if diff > 600:
        return math.floor(diff / 60)
    return 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    diff_sec = int((punch_out - punch_in).total_seconds())
    return float((Decimal(diff_sec) / Decimal(3600)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

def half_up(val: float, decimals: int = 2) -> float:
    return float(Decimal(str(val)).quantize(Decimal('1.' + '0' * decimals), rounding=ROUND_HALF_UP))

def get_working_days(joined_on: str, month: str) -> int:
    y, m = map(int, month.split("-"))
    _, days_in_month = calendar.monthrange(y, m)
    
    start_date = date(y, m, 1)
    if joined_on > start_date.isoformat():
        if joined_on > date(y, m, days_in_month).isoformat():
            return 0
        y_j, m_j, d_j = map(int, joined_on.split("-"))
        start_date = date(y_j, m_j, d_j)
        
    end_date = date(y, m, days_in_month)
    
    working_days = 0
    current_date = start_date
    while current_date <= end_date:
        if current_date.weekday() < 5:  # 0-4 are Mon-Fri
            working_days += 1
        current_date += timedelta(days=1)
        
    return working_days

def build_employee_monthly_pipeline(emp_code: str, month: str):
    y, m = map(int, month.split("-"))
    _, days_in_month = calendar.monthrange(y, m)
    
    start_date = f"{y:04d}-{m:02d}-01"
    end_date = f"{y:04d}-{m:02d}-{days_in_month:02d}"
    
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }
        },
        {
            "$addFields": {
                "date_obj": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d"
                    }
                }
            }
        },
        {
            "$addFields": {
                "day_of_week": {"$isoDayOfWeek": "$date_obj"}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$lte": ["$day_of_week", 5]}
                            ]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]

def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    y, m = map(int, month.split("-"))
    _, days_in_month = calendar.monthrange(y, m)
    start_date = f"{y:04d}-{m:02d}-01"
    end_date = f"{y:04d}-{m:02d}-{days_in_month:02d}"

    match_stage = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department
        
    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"e_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$e_code"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]}
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "date_obj": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {"$isoDayOfWeek": "$date_obj"}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$addFields": {
                "emp_present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$lte": ["$$l.day_of_week", 5]}
                                    ]},
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1]},
                                    0
                                ]
                            }
                        }
                    }
                },
                "emp_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "cond": {"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}
                        }
                    }
                },
                "emp_total_late_minutes": {"$sum": "$logs.late_minutes"},
                "emp_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "cond": {"$eq": ["$$this.status", "LEAVE"]}
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "cond": {"$eq": ["$$this.status", "ON_DUTY"]}
                        }
                    }
                },
                "emp_work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$this.work_hours", None]}
                                        ]
                                    }
                                }
                            },
                            "as": "l",
                            "in": "$$l.work_hours"
                        }
                    }
                },
                "emp_work_hours_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "cond": {
                                "$and": [
                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$$this.work_hours", None]}
                                ]
                            }
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "dept_work_hours_sum": {"$sum": "$emp_work_hours_sum"},
                "dept_work_hours_count": {"$sum": "$emp_work_hours_count"}
            }
        },
        {
            "$addFields": {
                "department": "$_id",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$dept_work_hours_count", 0]},
                        {"$divide": ["$dept_work_hours_sum", "$dept_work_hours_count"]},
                        None
                    ]
                }
            }
        },
        {
            "$sort": {"department": 1}
        },
        {
            "$project": {
                "_id": 0,
                "dept_work_hours_sum": 0,
                "dept_work_hours_count": 0
            }
        }
    ]

def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None):
    y, m = map(int, month.split("-"))
    _, days_in_month = calendar.monthrange(y, m)
    start_date = f"{y:04d}-{m:02d}-01"
    end_date = f"{y:04d}-{m:02d}-{days_in_month:02d}"
    
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {
            "$unwind": "$emp"
        }
    ]
    
    if department:
        pipeline.append({
            "$match": {
                "emp.department": department
            }
        })
        
    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                }
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1
            }
        }
    ])
    
    return pipeline

def build_department_trend_pipeline(department: str, from_date: str, to_date: str):
    start_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    end_dt = (datetime.fromisoformat(to_date) + timedelta(days=1)).replace(tzinfo=timezone.utc)
    
    return [
        {"$documents": [{"dummy": 1, "date_obj": start_dt}]},
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_obj",
                        "timezone": "UTC"
                    }
                },
                "day_of_week": {"$isoDayOfWeek": "$date_obj"},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date_obj"}, 5]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "emp_data"
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$emp_data.count", 0]}, 0]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$date", "$$d"]
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp"
                        }
                    },
                    {
                        "$unwind": "$emp"
                    },
                    {
                        "$match": {
                            "emp.department": department
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1]},
                                    0
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "cond": {"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": [
                            {"$eq": ["$is_working_day", True]},
                            {"$gt": ["$headcount", 0]}
                        ]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        }
    ]

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r'^EMP\d{4,6}$')
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str = Field("18:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: str # YYYY-MM-DD
    
    @model_validator(mode='after')
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: str = "PRESENT"
    
    @model_validator(mode='after')
    def validate_status(self):
        if self.status not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("Invalid status for punch-in")
        return self

class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)

class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    
    @model_validator(mode='after')
    def validate_body(self):
        if self.status in ("ABSENT", "LEAVE") and (self.punch_in is not None or self.punch_out is not None):
            raise ValueError("Cannot supply punch times for ABSENT or LEAVE")
        return self

def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

def employee_to_api(doc: dict) -> dict:
    doc.pop("_id", None)
    if "created_at" in doc:
        doc["created_at"] = to_ms(doc["created_at"])
    return doc

def attendance_to_api(doc: dict) -> dict:
    doc.pop("_id", None)
    if doc.get("punch_in"): doc["punch_in"] = to_ms(doc["punch_in"])
    if doc.get("punch_out"): doc["punch_out"] = to_ms(doc["punch_out"])
    if "history" not in doc: doc["history"] = []
    if "half_day" not in doc: doc["half_day"] = False
    
    for h in doc["history"]:
        if "at" in h and isinstance(h["at"], datetime):
            h["at"] = to_ms(h["at"])
        if "punch_in" in h.get("changes", {}):
            if h["changes"]["punch_in"]["from"]: h["changes"]["punch_in"]["from"] = to_ms(h["changes"]["punch_in"]["from"])
            if h["changes"]["punch_in"]["to"]: h["changes"]["punch_in"]["to"] = to_ms(h["changes"]["punch_in"]["to"])
        if "punch_out" in h.get("changes", {}):
            if h["changes"]["punch_out"]["from"]: h["changes"]["punch_out"]["from"] = to_ms(h["changes"]["punch_out"]["from"])
            if h["changes"]["punch_out"]["to"]: h["changes"]["punch_out"]["to"] = to_ms(h["changes"]["punch_out"]["to"])
    return doc

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        ensure_indexes()
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_api(doc)

@app.get("/employees")
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size))
    return {"items": [employee_to_api(d) for d in items], "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Unknown employee")
        
    ts = truncate_seconds(from_ms(body.punched_at)) if body.punched_at else truncate_seconds(datetime.now(timezone.utc))
    
    d, start_dt, end_dt = get_attendance_date_and_shift(ts, emp["shift_start"], emp["shift_end"])
    
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, start_dt),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
        
    return attendance_to_api(doc)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from > date_to")
        
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
        
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size))
    
    return {"items": [attendance_to_api(d) for d in docs], "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Unknown employee")
        
    ts = truncate_seconds(from_ms(body.punched_at)) if body.punched_at else truncate_seconds(datetime.now(timezone.utc))
    
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if not record:
        raise HTTPException(404, "No punch-in found")
        
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
        
    if ts <= record["punch_in"]:
        raise HTTPException(422, "punched_at must be after punch_in")
        
    if (ts - record["punch_in"]).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")
        
    work_hours = compute_work_hours(record["punch_in"], ts)
    half_day = work_hours < 4.50
    
    _, _, end_dt = get_attendance_date_and_shift(record["punch_in"], emp["shift_start"], emp["shift_end"])
    
    overtime_diff = (ts - end_dt).total_seconds()
    overtime_minutes = math.floor(overtime_diff / 60) if overtime_diff >= 30 * 60 else 0
    
    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day
        }},
        return_document=ReturnDocument.AFTER
    )
    if not res:
        raise HTTPException(409, "Already punched out concurrently")
        
    return attendance_to_api(res)

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Unknown employee")
        
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Record not found")
        
    history_len = len(record.get("history") or [])
    
    new_status = body.status if body.status is not None else record["status"]
    
    changes = {}
    def track_change(field, old, new):
        if old != new:
            changes[field] = {"from": old, "to": new}
            return True
        return False

    track_change("status", record["status"], new_status)
    
    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = truncate_seconds(from_ms(body.punch_in)) if body.punch_in is not None else record.get("punch_in")
        new_punch_out = truncate_seconds(from_ms(body.punch_out)) if body.punch_out is not None else record.get("punch_out")
        
        if not new_punch_in:
            raise HTTPException(422, "Presence status requires a punch_in")
            
        d, start_dt, end_dt = get_attendance_date_and_shift(new_punch_in, emp["shift_start"], emp["shift_end"])
        if d != date:
            raise HTTPException(422, "punch_in must remain on the record's attendance date")
            
        if new_punch_out:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, "punch_out cannot be > 24h after punch_in")

    track_change("punch_in", record.get("punch_in"), new_punch_in)
    track_change("punch_out", record.get("punch_out"), new_punch_out)
    
    if new_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_work = None
        new_overtime = 0
        new_half = False
    else:
        _, start_dt, end_dt = get_attendance_date_and_shift(new_punch_in, emp["shift_start"], emp["shift_end"])
        new_late = compute_late_minutes(new_punch_in, start_dt)
        if new_punch_out:
            new_work = compute_work_hours(new_punch_in, new_punch_out)
            new_half = new_work < 4.50
            overtime_diff = (new_punch_out - end_dt).total_seconds()
            new_overtime = math.floor(overtime_diff / 60) if overtime_diff >= 30 * 60 else 0
        else:
            new_work = None
            new_half = False
            new_overtime = 0

    track_change("late_minutes", record.get("late_minutes", 0), new_late)
    track_change("work_hours", record.get("work_hours"), new_work)
    track_change("overtime_minutes", record.get("overtime_minutes", 0), new_overtime)
    track_change("half_day", record.get("half_day", False), new_half)
    
    if not changes:
        raise HTTPException(422, "No actual changes made")
        
    history_entry = {
        "at": truncate_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }
    
    update_doc = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "late_minutes": new_late,
        "work_hours": new_work,
        "overtime_minutes": new_overtime,
        "half_day": new_half
    }
    
    res = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"], 
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]}
        },
        {
            "$set": update_doc,
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )
    
    if not res:
        raise HTTPException(409, "Concurrent modification detected")
        
    return attendance_to_api(res)

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Unknown employee")
        
    working_days = get_working_days(emp["joined_on"], month)
    
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))
    
    if res:
        data = res[0]
    else:
        data = {
            "present_days": 0.0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0
        }
        
    if working_days > 0:
        pct = half_up((data["present_days"] / working_days) * 100, 2)
    else:
        pct = None
        
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": data["present_days"],
        "leave_days": data["leave_days"],
        "late_count": data["late_count"],
        "total_late_minutes": data["total_late_minutes"],
        "total_overtime_minutes": data["total_overtime_minutes"],
        "attendance_pct": pct
    }

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None
):
    pipeline = build_department_summary_pipeline(month, department)
    res = list(db.employees.aggregate(pipeline))
    
    for r in res:
        if r.get("avg_work_hours") is not None:
            r["avg_work_hours"] = half_up(r["avg_work_hours"], 2)
            
    return {
        "month": month,
        "items": res
    }

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    res = list(db.attendance_logs.aggregate(pipeline))
    return {
        "month": month,
        "items": res
    }

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r'^\d{4}-\d{2}-\d{2}$'),
    to_date: str = Query(..., alias="to", pattern=r'^\d{4}-\d{2}-\d{2}$')
):
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Unknown department")
        
    if from_date > to_date:
        raise HTTPException(422, "to < from")
        
    start_dt = datetime.fromisoformat(from_date)
    end_dt = datetime.fromisoformat(to_date)
    if (end_dt - start_dt).days > 92:
        raise HTTPException(422, "range > 92 days")
        
    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    res = list(db.aggregate(pipeline))
    
    for r in res:
        if r.get("attendance_rate") is not None:
            r["attendance_rate"] = half_up(r["attendance_rate"], 4)
        if r.get("moving_avg_7d") is not None:
            r["moving_avg_7d"] = half_up(r["moving_avg_7d"], 4)
            
    return {
        "department": department,
        "items": res
    }

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r'^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$'),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status: q["status"] = status
        skip = (page - 1) * page_size
        command_doc = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size
        }
        res = db.command("explain", command_doc, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        pipeline = build_department_summary_pipeline(month, department)
        res = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        res = db.command("explain", {"aggregate": 1, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": res}

    else:
        raise HTTPException(422, "Invalid endpoint")

