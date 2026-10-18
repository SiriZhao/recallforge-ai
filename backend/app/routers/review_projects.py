from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import secrets
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine, delete, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.config import settings
from app.schemas.review import OCRConfig
from app.services.cloud_runtime import runtime_dir
from app.services.file_parser import parse_file

router = APIRouter(prefix="/api/review-projects", tags=["review-projects"])

ALLOWED_EXTENSIONS = {".pptx", ".pdf", ".docx", ".md", ".png", ".jpg", ".jpeg", ".txt"}
MATERIAL_ROLES = {"slides", "textbook", "notes", "syllabus", "past_exam", "answer_key", "mistakes", "other"}
MODULES = ("overview", "priority_map", "study_guide", "past_exams", "question_types", "mock_exam", "anki", "pitfalls", "sprint_plan")


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ReviewProject(Base):
    __tablename__ = "review_projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    course_name: Mapped[str] = mapped_column(String(160))
    exam_date: Mapped[str] = mapped_column(String(20), default="")
    exam_type: Mapped[str] = mapped_column(String(40), default="unknown")
    daily_minutes: Mapped[int] = mapped_column(Integer, default=90)
    mastery_level: Mapped[str] = mapped_column(String(40), default="forgotten")
    target_score: Mapped[str] = mapped_column(String(40), default="")
    focus: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ProjectFile(Base):
    __tablename__ = "project_files"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("review_projects.id", ondelete="CASCADE"), index=True)
    saved_filename: Mapped[str] = mapped_column(String(255), unique=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(40), default="other")
    status: Mapped[str] = mapped_column(String(30), default="uploaded")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    text_length: Mapped[int] = mapped_column(Integer, default=0)


class Report(Base):
    __tablename__ = "review_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("review_projects.id", ondelete="CASCADE"), index=True)
    content_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ReportModule(Base):
    __tablename__ = "report_modules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("review_reports.id", ondelete="CASCADE"), index=True)
    module_type: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="waiting")
    content_json: Mapped[str] = mapped_column(Text, default="{}")
    quality_score: Mapped[int] = mapped_column(Integer, default=0)


class ReportVersion(Base):
    __tablename__ = "report_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("review_reports.id", ondelete="CASCADE"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    content_json: Mapped[str] = mapped_column(Text)
    change_summary: Mapped[str] = mapped_column(String(255), default="initial generation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


engine = create_engine(settings.database_url, connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class WorkspaceCreate(BaseModel):
    workspace_id: str | None = None
    workspace_secret: str | None = None


class ProjectCreate(BaseModel):
    course_name: str = Field(min_length=1, max_length=160)
    exam_date: str = ""
    exam_type: str = "unknown"
    daily_minutes: int = Field(default=90, ge=15, le=960)
    mastery_level: str = "forgotten"
    target_score: str = ""
    focus: str = ""


class ModuleUpdate(BaseModel):
    content: dict
    change_summary: str = Field(default="manual update", max_length=255)


class FileRoleUpdate(BaseModel):
    role: str


def initialize_review_database() -> None:
    Base.metadata.create_all(engine)


def database_ready() -> bool:
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        return True
    except Exception:
        return False


def db_session():
    with SessionLocal() as db:
        yield db


def hashed(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def current_workspace(
    workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
    workspace_secret: str | None = Header(default=None, alias="X-Workspace-Secret"),
    db: Session = Depends(db_session),
) -> Workspace:
    if not workspace_id or not workspace_secret:
        raise HTTPException(status_code=401, detail="工作空间标识缺失，请刷新后重试。")
    workspace = db.get(Workspace, workspace_id)
    if not workspace or not secrets.compare_digest(workspace.secret_hash, hashed(workspace_secret)):
        raise HTTPException(status_code=404, detail="工作空间不存在或无权访问。")
    return workspace


def owned_project(db: Session, workspace: Workspace, project_id: str) -> ReviewProject:
    project = db.scalar(select(ReviewProject).where(ReviewProject.id == project_id, ReviewProject.workspace_id == workspace.id))
    if not project:
        raise HTTPException(status_code=404, detail="复习项目不存在。")
    return project


def detect_role(name: str) -> str:
    lower = name.lower()
    for role, markers in {
        "past_exam": ("试卷", "真题", "历年", "期末", "exam", "past"),
        "answer_key": ("答案", "解析", "answer", "solution"),
        "syllabus": ("纲要", "范围", "大纲", "syllabus"),
        "notes": ("笔记", "note"), "textbook": ("教材", "textbook"),
        "mistakes": ("错题", "wrong"), "slides": ("课件", "slide", "ppt"),
    }.items():
        if any(marker in lower for marker in markers):
            return role
    return "other"


@router.post("/workspace", status_code=201)
def create_workspace(payload: WorkspaceCreate, db: Session = Depends(db_session)):
    workspace_id = payload.workspace_id or str(uuid4())
    workspace_secret = payload.workspace_secret or secrets.token_urlsafe(32)
    existing = db.get(Workspace, workspace_id)
    if existing:
        if not secrets.compare_digest(existing.secret_hash, hashed(workspace_secret)):
            raise HTTPException(status_code=409, detail="工作空间标识冲突。")
    else:
        db.add(Workspace(id=workspace_id, secret_hash=hashed(workspace_secret)))
        db.commit()
    return {"workspace_id": workspace_id, "workspace_secret": workspace_secret}


@router.get("/projects")
def list_projects(workspace: Workspace = Depends(current_workspace), db: Session = Depends(db_session)):
    projects = db.scalars(select(ReviewProject).where(ReviewProject.workspace_id == workspace.id).order_by(ReviewProject.created_at.desc())).all()
    project_ids = [item.id for item in projects]
    files_by_project: dict[str, list[dict]] = {}
    if project_ids:
        files = db.scalars(select(ProjectFile).where(ProjectFile.project_id.in_(project_ids))).all()
        for file in files:
            files_by_project.setdefault(file.project_id, []).append({
                "id": file.id, "saved_filename": file.saved_filename,
                "original_filename": file.original_filename, "role": file.role,
                "pages": file.page_count,
            })
    return [{
        "id": item.id, "course_name": item.course_name, "exam_date": item.exam_date,
        "exam_type": item.exam_type, "daily_minutes": item.daily_minutes,
        "mastery_level": item.mastery_level, "target_score": item.target_score,
        "focus": item.focus, "created_at": item.created_at.replace(tzinfo=timezone.utc).isoformat(),
        "files": files_by_project.get(item.id, []),
    } for item in projects]


@router.post("/projects", status_code=201)
def create_project(payload: ProjectCreate, workspace: Workspace = Depends(current_workspace), db: Session = Depends(db_session)):
    project = ReviewProject(id=str(uuid4()), workspace_id=workspace.id, **payload.model_dump())
    db.add(project)
    db.commit()
    return {"id": project.id, **payload.model_dump()}


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str, workspace: Workspace = Depends(current_workspace), db: Session = Depends(db_session)) -> Response:
    project = owned_project(db, workspace, project_id)
    from app.routers.learning import PracticeAttempt, ConceptMastery, ReviewSchedule, ExamAttempt
    for learning_model in (PracticeAttempt, ConceptMastery, ReviewSchedule, ExamAttempt):
        db.execute(delete(learning_model).where(learning_model.project_id == project.id))
    saved_filenames = list(db.scalars(select(ProjectFile.saved_filename).where(ProjectFile.project_id == project.id)).all())
    report_ids = list(db.scalars(select(Report.id).where(Report.project_id == project.id)).all())
    if report_ids:
        db.execute(delete(ReportVersion).where(ReportVersion.report_id.in_(report_ids)))
        db.execute(delete(ReportModule).where(ReportModule.report_id.in_(report_ids)))
        db.execute(delete(Report).where(Report.id.in_(report_ids)))
    db.execute(delete(ProjectFile).where(ProjectFile.project_id == project.id))
    db.delete(project)
    db.commit()

    upload_root = runtime_dir(settings.upload_dir).resolve()
    for saved_filename in saved_filenames:
        candidate = (upload_root / Path(saved_filename).name).resolve()
        try:
            candidate.relative_to(upload_root)
        except ValueError:
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass
    return Response(status_code=204)


@router.post("/projects/{project_id}/upload", status_code=201)
async def upload_project_files(
    project_id: str,
    files: list[UploadFile] = File(...),
    workspace: Workspace = Depends(current_workspace),
    db: Session = Depends(db_session),
):
    project = owned_project(db, workspace, project_id)
    if not files or len(files) > settings.max_files_per_request:
        raise HTTPException(status_code=413, detail="上传文件数量超出限制。")
    upload_dir = runtime_dir(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    created = []
    for file in files:
        original_filename = Path(file.filename or "upload").name
        suffix = Path(original_filename).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail="不支持该文件类型。")
        content = await file.read()
        if not content or len(content) > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="文件为空或超过大小限制。")
        saved_filename = f"{uuid4().hex}{suffix}"
        path = upload_dir / saved_filename
        path.write_bytes(content)
        parsed = parse_file(path, OCRConfig())
        role = detect_role(original_filename)
        item = ProjectFile(id=str(uuid4()), project_id=project.id, saved_filename=saved_filename, original_filename=original_filename, role=role, status="parsed", page_count=len(parsed.pages), text_length=len(parsed.raw_text))
        db.add(item)
        created.append({"id": item.id, "saved_filename": saved_filename, "original_filename": original_filename, "role": role, "pages": item.page_count})
    db.commit()
    return {"files": created}


@router.patch("/projects/{project_id}/files/{file_id}")
def update_project_file(project_id: str, file_id: str, payload: FileRoleUpdate, workspace: Workspace = Depends(current_workspace), db: Session = Depends(db_session)):
    owned_project(db, workspace, project_id)
    item = db.scalar(select(ProjectFile).where(ProjectFile.id == file_id, ProjectFile.project_id == project_id))
    if not item:
        raise HTTPException(status_code=404, detail="资料不存在。")
    if payload.role not in MATERIAL_ROLES:
        raise HTTPException(status_code=400, detail="资料角色无效。")
    item.role = payload.role
    db.commit()
    return {"id": item.id, "role": item.role}


@router.get("/projects/{project_id}/diagnosis")
def project_diagnosis(project_id: str, workspace: Workspace = Depends(current_workspace), db: Session = Depends(db_session)):
    project = owned_project(db, workspace, project_id)
    files = db.scalars(select(ProjectFile).where(ProjectFile.project_id == project_id)).all()
    roles = {item.role for item in files}
    coverage = min(100, len(files) * 9 + (20 if "syllabus" in roles else 0) + (22 if "past_exam" in roles else 0) + (12 if "answer_key" in roles else 0) + (10 if "notes" in roles else 0))
    missing = [label for role, label in (("syllabus", "课程纲要或明确考试范围"), ("past_exam", "往年试卷"), ("answer_key", "答案解析"), ("mistakes", "个人错题")) if role not in roles]
    strategy = []
    if "past_exam" in roles: strategy.append("优先依据往年题归纳题型、考点和模拟卷结构。")
    if "syllabus" in roles: strategy.append("使用课程纲要限制考试边界，避免扩写无关内容。")
    if "notes" in roles: strategy.append("使用个人笔记识别教师强调与个人薄弱点。")
    if not strategy: strategy.append("先生成保守诊断；补充往年题或课程纲要后可提高模拟卷可信度。")
    return {"project": project.course_name, "files_processed": len(files), "text_characters": sum(item.text_length for item in files), "material_completeness": coverage, "roles": sorted(roles), "has_syllabus": "syllabus" in roles, "has_past_exams": "past_exam" in roles, "has_answers": "answer_key" in roles, "has_notes": "notes" in roles, "missing": missing, "risks": ["资料较少时模拟卷将采用保守练习题。"] if coverage < 45 else [], "recommended_strategy": strategy, "files": [{"id": item.id, "filename": item.original_filename, "role": item.role, "pages": item.page_count, "text_length": item.text_length} for item in files]}


@router.post("/projects/{project_id}/reports", status_code=201)
def create_report(project_id: str, workspace: Workspace = Depends(current_workspace), db: Session = Depends(db_session)):
    owned_project(db, workspace, project_id)
    report = Report(id=str(uuid4()), project_id=project_id)
    db.add(report)
    for module_type in MODULES:
        db.add(ReportModule(id=str(uuid4()), report_id=report.id, module_type=module_type, status="waiting"))
    db.commit()
    return {"report_id": report.id, "modules": [{"type": module_type, "status": "waiting"} for module_type in MODULES]}


@router.get("/reports/{report_id}/modules")
def list_modules(report_id: str, workspace: Workspace = Depends(current_workspace), db: Session = Depends(db_session)):
    report = db.scalar(select(Report).join(ReviewProject).where(Report.id == report_id, ReviewProject.workspace_id == workspace.id))
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在。")
    modules = db.scalars(select(ReportModule).where(ReportModule.report_id == report_id)).all()
    return [{"id": item.id, "type": item.module_type, "status": item.status, "content": json.loads(item.content_json), "quality_score": item.quality_score} for item in modules]


@router.put("/reports/{report_id}/modules/{module_id}")
def update_module(report_id: str, module_id: str, payload: ModuleUpdate, workspace: Workspace = Depends(current_workspace), db: Session = Depends(db_session)):
    report = db.scalar(select(Report).join(ReviewProject).where(Report.id == report_id, ReviewProject.workspace_id == workspace.id))
    module = db.scalar(select(ReportModule).where(ReportModule.id == module_id, ReportModule.report_id == report_id))
    if not report or not module:
        raise HTTPException(status_code=404, detail="报告模块不存在。")
    module.content_json = json.dumps(payload.content, ensure_ascii=False)
    module.status = "completed"
    module.quality_score = 75
    version_number = len(db.scalars(select(ReportVersion).where(ReportVersion.report_id == report_id)).all()) + 1
    db.add(ReportVersion(id=str(uuid4()), report_id=report_id, version_number=version_number, content_json=module.content_json, change_summary=payload.change_summary))
    db.commit()
    return {"id": module.id, "status": module.status, "version": version_number}
