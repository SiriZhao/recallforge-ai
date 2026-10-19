"""Private local OCR probes and budgeted real-course A/B runs.

All artifacts stay in the ignored backend/runtime/evaluation directory. Neither
source files nor keys are copied to public fixtures. No network in OCR mode.
"""
import argparse
import hashlib
import json
import re
import time
import sqlite3
from pathlib import Path
from unittest.mock import patch
from dotenv import dotenv_values
from pypdf import PdfReader
from app.schemas.review import LLMConfig, ParsedFile, ParsedPage, OCRConfig
from app.services.file_parser import render_pdf_page_to_image
from app.services.llm_capabilities import estimate_tokens
from app.services.evaluation_budget import EvaluationClient, BudgetedStageProvider


def redact(text):
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[REDACTED]', text)
    return '\n'.join(line for line in text.splitlines() if not re.search(r'姓名|学号|身份证|联系电话|班级|考生号', line))


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def ocr_probe(root, output):
    from rapidocr_onnxruntime import RapidOCR
    from app.services.ocr_providers.utils import image_to_bytes
    engine = RapidOCR()
    probes = []
    for subject, marker, page in [("probability", "概率论", 8), ("python", "Python", 1), ("botany", "植物学", 1), ("organic", "有机化学", 1)]:
        path = next(p for p in root.glob('*.pdf') if marker in p.name)
        identity = hashlib.sha256(path.read_bytes()).hexdigest()
        cache = output / f"ocr-{identity[:16]}-p{page}.json"
        if cache.exists():
            item = json.loads(cache.read_text(encoding="utf-8"))
        else:
            started = time.perf_counter()
            image = render_pdf_page_to_image(path, page)
            result, _ = engine(image_to_bytes(image))
            lines = [{"text": row[1], "confidence": float(row[2]), "bbox": [list(map(float, point)) for point in row[0]]} for row in (result or [])]
            item = {"subject": subject, "source_hash": identity, "page": page, "lines": lines,
                    "seconds": time.perf_counter()-started, "quality": "NOT_HUMAN_VERIFIED", "image_size": list(image.size)}
            save(cache, item)
        probes.append({"subject":subject,"page":page,"lines":len(item["lines"]),
            "mean_confidence":sum(row["confidence"] for row in item["lines"])/max(1,len(item["lines"])), "seconds":item["seconds"],
            "characters":sum(len(row["text"]) for row in item["lines"]), "quality":item["quality"]})
        print(json.dumps(probes[-1]), flush=True)
    save(output / "ocr-summary.json", probes)


def compare(root, output, subject, question_count=5):
    from app.services import generation_pipeline as pipeline
    source = next(root.glob('*合并.pdf'))
    pages = {"probability": [1,2], "python": [76,77,78], "botany": [111,112], "organic": [40,41]}[subject]
    reader = PdfReader(source)
    selected = [ParsedPage(page_number=i, source="text_extract", text=redact(reader.pages[i-1].extract_text() or ''), source_anchor=f"{subject}.pdf, p. {i}") for i in pages]
    parsed = ParsedFile(filename=f"{subject}.pdf", original_filename=f"{subject}.pdf", file_type=".pdf", path=f"{subject}.pdf", pages=selected, raw_text='\n'.join(p.text for p in selected))
    from app.services.exam_evidence import observed_exam_model
    questions=observed_exam_model([parsed],{parsed.filename:'past_exam'}).observed_questions
    chosen=[question for question in questions if question.completeness=='CANDIDATE_COMPLETE'][:question_count]
    if not chosen:
        raise RuntimeError('No complete question candidates; paid evaluation not started')
    selected=[]
    for number in sorted({q.source_refs[0].page for q in chosen}):
        text='\n\n'.join(q.text for q in chosen if q.source_refs[0].page==number)
        selected.append(ParsedPage(page_number=number,source='text_extract',text=text,source_anchor=f'{subject}.pdf, p. {number}'))
    parsed=parsed.model_copy(update={'pages':selected,'raw_text':'\n'.join(page.text for page in selected)})
    values = dotenv_values(Path(__file__).resolve().parents[2] / '.env')
    key = values.get('DEEPSEEK_API_KEY') or values.get('ERA_DEEPSEEK_API_KEY')
    client = EvaluationClient(output / 'paid-ledger.sqlite3', key=key)
    source_data = [{"source_ref":{"anchor":p.source_anchor,"page":p.page_number,"filename":parsed.filename},"text":p.text} for p in selected]
    source_hash = hashlib.sha256(json.dumps(source_data,sort_keys=True).encode()).hexdigest()
    objective = f"围绕提供的{len(chosen)}道真实试题整理课程知识、公式条件、讲解和解题步骤。区分题面观察与推断，不要虚构历史频次。输出4张问答卡片、2道练习题、2道不同的模拟题及具体答案、解析、rubric和来源。没有标准答案时明确未核实。最多规划两个教学单元。"
    manifest = {"subject":subject,"source_pages":pages,"source_hash":source_hash,"estimated_source_tokens":estimate_tokens(parsed.raw_text),
                "model":client.model,"objective":objective,"gold_status":"UNVERIFIED","human_evaluation":"NOT_PERFORMED",
                "budget_policy":{"total_usd":0.50,"per_arm_call_limit":16,"per_arm_input_tokens":200000,"per_arm_output_tokens":30000,"same_model_temperature":True},"source_data":source_data,
                "selected_observed_ids":[q.question_id for q in chosen]}
    save(output / f'{subject}-manifest.json',manifest)
    from app.schemas.learning_artifacts import GeneratedArtifactBundle
    from app.schemas.course_model import CourseModel
    baseline_messages = [{"role":"user","content":json.dumps({"task":objective,"materials":source_data,
        "format":"JSON with canonical_markdown, course_model, active_recall_cards, practice_questions, mock_exam. Do not invent supplied facts.",
        "output_contracts":{"course_model":CourseModel.model_json_schema(),"artifacts":GeneratedArtifactBundle.model_json_schema()}},ensure_ascii=False)}]
    # One initial answer plus an explicit review turn gives the chat workflow a
    # real opportunity to improve. Actual budgets are reported, not called equal.
    arm = subject+'-'+source_hash[:12]+'-chat'
    baseline = client.chat(arm,baseline_messages,6500)
    first = baseline['choices'][0]['message']['content']
    baseline_messages += [{"role":"assistant","content":first},{"role":"user","content":"检查覆盖、答案可作答性、公式条件、重复、来源和题库完整性，修正后返回完整最终JSON。"}]
    baseline_final = client.chat(arm,baseline_messages,6500)
    save(output/f'{subject}-chat.json',baseline_final)
    checkpoints_path = output/f'{subject}-checkpoints.json'
    saved = json.loads(checkpoints_path.read_text(encoding='utf-8')) if checkpoints_path.exists() else {}
    def checkpoint(key, chapter, chunk, status, content, code, message):
        saved[key]={"status":status,"content":content}
        save(checkpoints_path,saved)
    provider = BudgetedStageProvider(client,subject+'-'+source_hash[:12]+'-recallforge')
    try:
        with patch.object(pipeline,'get_llm_provider',return_value=provider):
            result,stats=pipeline.generate_hierarchical_report([parsed],None,LLMConfig(enabled=True,provider='deepseek',model=client.model),
                job_id=source_hash,title=subject,study_goal='balanced',exam_type='closed_book',learner_context={'daily_minutes':45,'learning_mode':'understand','focus':objective,
                    'artifact_targets':{'exam_questions':2,'practice_questions':2,'recall_cards':4,'max_units':2}},
                material_types={parsed.filename:'past_exam'},checkpoints=saved,checkpoint_callback=checkpoint)
        save(output/f'{subject}-recallforge.json',{'result':result.report.model_dump(mode='json'),'status':result.llm_status,'stats':vars(stats)})
        state=result.llm_status
    except Exception as exc:
        state=type(exc).__name__
        from app.services.evaluation_budget import EvaluationBudgetError
        save(output/f'{subject}-failure.json',{'error_type':state,'error_code':str(exc) if isinstance(exc,EvaluationBudgetError) else None,'paid_summary':client.summary()})
    print(json.dumps({'subject':subject,'recallforge_status':state,'budget':client.summary()},ensure_ascii=True),flush=True)


def match_baseline(output,subject):
    """Give the generic chat arm the same spending opportunity, not fewer turns."""
    manifest=json.loads((output/f'{subject}-manifest.json').read_text(encoding='utf-8'))
    arm=subject+'-'+manifest['source_hash'][:12]+'-chat'
    engine_arm=subject+'-'+manifest['source_hash'][:12]+'-recallforge'
    values=dotenv_values(Path(__file__).resolve().parents[2]/'.env')
    client=EvaluationClient(output/'paid-ledger.sqlite3',key=values.get('DEEPSEEK_API_KEY') or values.get('ERA_DEEPSEEK_API_KEY'))
    for _ in range(14):
        with client.connect() as db:
            target=db.execute('SELECT coalesce(sum(coalesce(charge,reserve)),0) FROM evaluation_calls WHERE arm=?',(engine_arm,)).fetchone()[0]
            spent=db.execute('SELECT coalesce(sum(coalesce(charge,reserve)),0) FROM evaluation_calls WHERE arm=?',(arm,)).fetchone()[0]
            rows=db.execute('SELECT request_json,response_json FROM evaluation_calls WHERE arm=? AND status="completed" ORDER BY rowid',(arm,)).fetchall()
        if not rows or spent>=target*.95:break
        previous_request=json.loads(rows[-1][0]);previous_response=json.loads(rows[-1][1])
        messages=previous_request['messages']+[
            {'role':'assistant','content':previous_response['choices'][0]['message']['content']},
            {'role':'user','content':'在同一份资料范围内再审查并改进这份完整学习包：核对每条引用，区分材料事实与推断；处理不确定或缺失的答案，补充必要的解释、条件与常见误区；确保卡片和两套题可用且不重复。返回完整最终 JSON，不要增加没有材料依据的确定事实。'}]
        used_output=sum(json.loads(row[1]).get('usage',{}).get('completion_tokens',0) for row in rows)
        remaining=30000-used_output
        if remaining<1000:break
        try:
            final=client.chat(arm,messages,min(4500,remaining))
        except Exception as exc:
            print(json.dumps({'baseline_matching_stopped':type(exc).__name__,'budget':client.summary()}),flush=True)
            break
        if final['choices'][0].get('finish_reason')=='stop':save(output/f'{subject}-chat.json',final)
    print(json.dumps({'baseline_matching_complete':True,'budget':client.summary()}),flush=True)


def patch_baseline(output,subject):
    """Avoid penalizing chat for repeatedly reprinting an entire learning pack."""
    from copy import deepcopy
    from app.services.generation_pipeline import _coerce_stage_json
    manifest=json.loads((output/f'{subject}-manifest.json').read_text(encoding='utf-8'))
    raw=json.loads((output/f'{subject}-chat.json').read_text(encoding='utf-8'))
    original=_coerce_stage_json(raw['choices'][0]['message']['content'])
    arm=subject+'-'+manifest['source_hash'][:12]+'-chat'
    values=dotenv_values(Path(__file__).resolve().parents[2]/'.env')
    client=EvaluationClient(output/'paid-ledger.sqlite3',key=values.get('DEEPSEEK_API_KEY') or values.get('ERA_DEEPSEEK_API_KEY'))
    content=json.dumps({'task':'Review this general-chat study pack against exactly the same exam evidence. Return JSON corrections [{path:[...],value:...}] only for important defects; do not reprint the full pack. Improve explanations, uncertainty and source grounding. Do not turn inferred knowledge into observed course facts.',
        'materials':manifest['source_data'],'current_pack':original},ensure_ascii=False)
    response=client.chat(arm,[{'role':'user','content':content}],2500)
    if response['choices'][0].get('finish_reason')!='stop':
        print(json.dumps({'patch_status':'TRUNCATED','budget':client.summary()}));return
    patch=_coerce_stage_json(response['choices'][0]['message']['content'])
    result=deepcopy(original)
    for change in patch.get('corrections',[]):
        path=change.get('path',[])
        if not path or path[0] not in {'canonical_markdown','course_model','active_recall_cards','practice_questions','mock_exam'}:
            raise ValueError('Baseline patch outside output scope')
        target=result
        for key in path[:-1]:target=target[key]
        target[path[-1]]=change['value']
    save(output/f'{subject}-chat-materialized.json',{'source_hash':manifest['source_hash'],'output':result,
        'source_response_ids':[raw.get('id'),response.get('id')],'method':'AI leaf revisions applied locally; raw responses retained in ledger'})
    print(json.dumps({'patch_status':'COMPLETE','changes':len(patch.get('corrections',[])),'budget':client.summary()}))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['ocr','compare','match-baseline','patch-baseline'])
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--subject',choices=['probability','python','botany','organic'],default='probability')
    parser.add_argument('--question-count',type=int,choices=range(1,11),default=5)
    args=parser.parse_args()
    output=Path(__file__).resolve().parents[2]/'runtime'/'evaluation'
    output.mkdir(parents=True,exist_ok=True)
    if args.mode=='match-baseline':match_baseline(output,args.subject)
    elif args.mode=='patch-baseline':patch_baseline(output,args.subject)
    else:(ocr_probe if args.mode=='ocr' else lambda root,out:compare(root,out,args.subject,args.question_count))(args.source.resolve(),output)


if __name__=='__main__':
    main()
