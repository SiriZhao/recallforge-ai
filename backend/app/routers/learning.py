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


class MisconceptionRecord(Base):
    """Answer-linked evidence, not an inferred psychological diagnosis."""
    __tablename__="learning_misconceptions"
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    project_id:Mapped[str]=mapped_column(String(36),index=True)
    attempt_id:Mapped[str]=mapped_column(String(36),index=True)
    knowledge_id:Mapped[str]=mapped_column(String(100),index=True)
    category:Mapped[str]=mapped_column(String(40))
    basis:Mapped[str]=mapped_column(Text)


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
    errors=assessment["errors"]
    if not errors and assessment["score"] is not None and assessment["score"] < .8:
        errors=[{"category":"uncertain","basis":"Low reported/reference-relative score; the causal error category is unknown."}]
    for knowledge_id in item.get("knowledge_ids",[]):
        for error in errors:
            db.add(MisconceptionRecord(id=str(uuid4()),project_id=project.id,attempt_id=attempt_id,
                knowledge_id=knowledge_id,category=error["category"],basis=error["basis"]))
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
    recommendation=recommend(concepts,states,settings)
    priorities={row["knowledge_id"]:index for index,row in enumerate(recommendation["items"])}
    last={attempt["item_id"]:attempt for attempt in attempts if attempt["kind"]=="practice"}
    practice=report.get("practice_questions",[])
    def rank(item):
        previous=last.get(item.get("question_id"),{}).get("assessment",{})
        score=previous.get("score")
        return (min((priorities[key] for key in item.get("knowledge_ids",[]) if key in priorities),default=len(priorities)),
            score is not None and score>=.8, score if score is not None else .5)
    recommendation["practice_item_ids"]=[item["question_id"] for item in sorted(practice,key=rank) if item.get("question_id")]
    # Historical errors are immutable; current mastery is returned separately so
    # a later success does not erase the evidence trail or label it a current error.
    mistakes=db.scalars(select(MisconceptionRecord).where(MisconceptionRecord.project_id==project.id)).all()
    history=db.scalars(select(PracticeAttempt).where(PracticeAttempt.project_id==project.id)).all()
    by_attempt={row.id: (row,json.loads(row.content_json)) for row in history}
    latest={}
    for row,data in by_attempt.values():
        key=(row.job_id,row.kind,row.item_id)
        if key not in latest or data['created_at']>latest[key]['created_at']:
            latest[key]=data
    diagnostics={}
    for mistake in mistakes:
        entry=diagnostics.setdefault(mistake.knowledge_id,{"record_count":0,"categories":{},
            "current_review_count":0,"subsequent_success_count":0,"records":[],
            "basis":"historical answer evidence, not a verified causal diagnosis"})
        entry["record_count"]+=1
        entry["categories"][mistake.category]=entry["categories"].get(mistake.category,0)+1
        origin=by_attempt.get(mistake.attempt_id)
        status='NEEDS_REVIEW'
        followup=None
        if origin:
            row,data=origin
            followup=latest.get((row.job_id,row.kind,row.item_id))
            score=followup.get('assessment',{}).get('score') if followup else None
            if followup and followup['created_at']>data['created_at']:
                if score is None: status='PENDING_SELF_REVIEW'
                elif score>=.8: status='SUBSEQUENT_SUCCESS'
        entry['subsequent_success_count' if status=='SUBSEQUENT_SUCCESS' else 'current_review_count']+=1
        entry['records'].append({'attempt_id':mistake.attempt_id,'category':mistake.category,
            'status':status,'latest_attempt_id':followup.get('attempt_id') if followup else None,
            'basis':'Same job, mode and item follow-up only; later success is not proof of misconception resolution'})
    return {"attempts":attempts,"mastery":states,"recommendation":recommendation,"diagnostics":diagnostics}
