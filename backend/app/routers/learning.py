"""Workspace-owned practice/recall persistence, assessment and next-step planning."""
import json
import hashlib
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from app.routers.review_projects import Base, Workspace, current_workspace, db_session, owned_project
from app.services import generation_store
from app.services.learning_assessment import assess_answer, update_mastery, recommend

router=APIRouter(prefix="/api/review-projects/learning",tags=["learning"])


class PracticeAttempt(Base):
    __tablename__="learning_attempts"
    __table_args__=(UniqueConstraint("job_id","nonce"),)
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    project_id:Mapped[str]=mapped_column(String(36),index=True)
    job_id:Mapped[str]=mapped_column(String(64),index=True)
    nonce:Mapped[str]=mapped_column(String(64))
    item_id:Mapped[str]=mapped_column(String(100))
    kind:Mapped[str]=mapped_column(String(20))
    payload_hash:Mapped[str]=mapped_column(String(64))
    content_json:Mapped[str]=mapped_column(Text)


class ConceptMastery(Base):
    __tablename__="concept_mastery"
    project_id:Mapped[str]=mapped_column(String(36),primary_key=True)
    knowledge_id:Mapped[str]=mapped_column(String(100),primary_key=True)
    state_json:Mapped[str]=mapped_column(Text)


class ReviewSchedule(Base):
    __tablename__="learning_review_schedules"
    project_id:Mapped[str]=mapped_column(String(36),primary_key=True)
    knowledge_id:Mapped[str]=mapped_column(String(100),primary_key=True)
    due_at:Mapped[str]=mapped_column(String(40))


class ExamAttempt(Base):
    __tablename__="learning_exam_attempts"
    id:Mapped[str]=mapped_column(String(64),primary_key=True)
    project_id:Mapped[str]=mapped_column(String(36),index=True)
    job_id:Mapped[str]=mapped_column(String(64))
    content_json:Mapped[str]=mapped_column(Text)


class AttemptRequest(BaseModel):
    item_id:str=Field(min_length=1,max_length=100)
    kind:str=Field(pattern="^(practice|exam|recall)$")
    answer:str=Field(default="",max_length=20000)
    nonce:str=Field(min_length=1,max_length=64)
    duration_seconds:float|None=Field(default=None,ge=0,le=86400)
    self_score:float|None=Field(default=None,ge=0,le=1)
    error_category:str|None=None
    exam_session_id:str|None=Field(default=None,max_length=64)


def owned_job(db,workspace,job_id):
    job=generation_store.get_job(job_id)
    if not job or not job.get("project_id"):
        raise HTTPException(404,"Learning job not found")
    project=owned_project(db,workspace,job["project_id"])
    if not job.get("result"):
        raise HTTPException(409,"No generated artifacts available")
    report=job["result"]["review_report"]
    return job,project,report


@router.post("/jobs/{job_id}/attempts")
def save_attempt(job_id:str,payload:AttemptRequest,workspace:Workspace=Depends(current_workspace),db:Session=Depends(db_session)):
    if db.get_bind().dialect.name == "sqlite":
        # Serialize read-modify-write mastery and nonce checks in SQLite.
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    job,project,report=owned_job(db,workspace,job_id)
    digest=hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
    existing=db.scalar(select(PracticeAttempt).where(PracticeAttempt.job_id==job_id,PracticeAttempt.nonce==payload.nonce))
    if existing:
        if existing.payload_hash!=digest:
            raise HTTPException(409,"Idempotency token already used for a different answer")
        return json.loads(existing.content_json)
    collection=report.get("anki_cards",[]) if payload.kind=="recall" else report.get("practice_questions",[]) if payload.kind=="practice" else report.get("mock_exam",{}).get("questions",[])
    item=next((item for item in collection if item.get("card_id" if payload.kind=="recall" else "question_id")==payload.item_id),None)
    if not item:
        raise HTTPException(404,"Generated item not found in this learning mode")
    try:
        assessment=assess_answer(item,payload.answer,self_score=payload.self_score,error_category=payload.error_category,card=payload.kind=="recall")
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    now=datetime.now(timezone.utc).isoformat()
    attempt_id=str(uuid4())
    result={"attempt_id":attempt_id,"item_id":payload.item_id,"kind":payload.kind,"answer":payload.answer,
            "duration_seconds":payload.duration_seconds,"assessment":assessment,"knowledge_ids":item.get("knowledge_ids",[]),"created_at":now}
    db.add(PracticeAttempt(id=attempt_id,project_id=project.id,job_id=job_id,nonce=payload.nonce,item_id=payload.item_id,
        kind=payload.kind,payload_hash=digest,content_json=json.dumps(result,ensure_ascii=False)))
    for knowledge_id in item.get("knowledge_ids",[]):
        state=db.get(ConceptMastery,(project.id,knowledge_id))
        prior=json.loads(state.state_json) if state else None
        updated=update_mastery(prior,assessment["score"],item.get("difficulty","medium"))
        if updated and assessment["score"] is not None:
            if state:state.state_json=json.dumps(updated)
            else:db.add(ConceptMastery(project_id=project.id,knowledge_id=knowledge_id,state_json=json.dumps(updated)))
            schedule=db.get(ReviewSchedule,(project.id,knowledge_id))
            if schedule:schedule.due_at=updated["next_review"]
            else:db.add(ReviewSchedule(project_id=project.id,knowledge_id=knowledge_id,due_at=updated["next_review"]))
    if payload.kind=="exam" and payload.exam_session_id:
        session=db.get(ExamAttempt,payload.exam_session_id)
        if session and (session.project_id!=project.id or session.job_id!=job_id):
            raise HTTPException(409,"Exam session belongs to another job")
        data=json.loads(session.content_json) if session else {"answers":{},"started_at":now}
        data["answers"][payload.item_id]=result
        data["status"]="completed" if len(data["answers"])==len(collection) else "in_progress"
        data["graded_count"]=sum(answer["assessment"]["score"] is not None for answer in data["answers"].values())
        data["total_questions"]=len(collection)
        if session:session.content_json=json.dumps(data,ensure_ascii=False)
        else:db.add(ExamAttempt(id=payload.exam_session_id,project_id=project.id,job_id=job_id,content_json=json.dumps(data,ensure_ascii=False)))
    db.commit()
    return result


@router.get("/jobs/{job_id}/progress")
def learning_progress(job_id:str,workspace:Workspace=Depends(current_workspace),db:Session=Depends(db_session)):
    job,project,report=owned_job(db,workspace,job_id)
    rows=db.scalars(select(PracticeAttempt).where(PracticeAttempt.project_id==project.id,PracticeAttempt.job_id==job_id)).all()
    states={row.knowledge_id:json.loads(row.state_json) for row in db.scalars(select(ConceptMastery).where(ConceptMastery.project_id==project.id)).all()}
    concepts=report.get("overview",{}).get("artifacts",{}).get("course_model",{}).get("concepts",[])
    settings={key:getattr(project,key) for key in ("exam_date","daily_minutes","target_score","mastery_level","focus","exam_type")}
    # The generation request's context remains useful for legacy projects.
    settings={**report.get("overview",{}).get("learner_context",{}),**settings}
    attempts=sorted([json.loads(row.content_json) for row in rows],key=lambda row:row["created_at"])
    return {"attempts":attempts,"mastery":states,"recommendation":recommend(concepts,states,settings)}
