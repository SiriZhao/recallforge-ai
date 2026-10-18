import {useRef,useState,useEffect} from 'react';
import type {GeneratedExamQuestion} from '../api/client';
import {Button} from '../design-system/Button';
import {useLearningProgress,type LearningAttempt} from '../hooks/useLearningProgress';
export function Questions({questions,mock,jobId}: {questions:GeneratedExamQuestion[];mock:boolean;jobId?:string}) {
  const [revealed,setRevealed]=useState<Record<number,boolean>>({});
  const [answers,setAnswers]=useState<Record<number,string>>({});
  const [assessments,setAssessments]=useState<Record<number,LearningAttempt>>({});
  const started=useRef(Date.now());
  const saving=useRef(new Set<number>());
  const learning=useLearningProgress(jobId);
  useEffect(()=>{
    if(!learning.progress)return;
    const restored:Record<number,string>={};const feedback:Record<number,LearningAttempt>={};
    for(const attempt of learning.progress.attempts){if(attempt.kind!==(mock?'exam':'practice'))continue;const index=questions.findIndex(q=>q.question_id===attempt.item_id);if(index>=0){restored[index]=attempt.answer;feedback[index]=attempt;}}
    setAnswers(current=>({...restored,...current}));setAssessments(feedback);
  },[learning.progress,questions,mock]);
  async function reveal(index:number){
    if(saving.current.has(index))return;
    setRevealed(old=>({...old,[index]:!old[index]}));
    if(revealed[index]||!answers[index]?.trim())return;
    saving.current.add(index);
    const saved=await learning.record(questions[index].question_id||'',mock?'exam':'practice',answers[index],(Date.now()-started.current)/1000);
    if(saved)setAssessments(old=>({...old,[index]:saved}));
    saving.current.delete(index);
  }
  async function selfAssess(index:number,key:string){
    if(saving.current.has(index)||!revealed[index]||!['1','2','3','4'].includes(key))return;
    saving.current.add(index);
    const saved=await learning.record(questions[index].question_id||'',mock?'exam':'practice',answers[index]||'',(Date.now()-started.current)/1000,(Number(key)-1)/3);
    if(saved)setAssessments(old=>({...old,[index]:saved}));
    saving.current.delete(index);
  }
  if(!questions.length)return <div className="empty-projects"><h2>暂无生成题目</h2><p>补充课程资料或往年试卷后重新生成。</p></div>;
  return <>
    <div className="document-label">{mock?'MOCK EXAM':'PRACTICE'}<span>{questions.length} 道题 · 自评练习</span></div>
    <h2>{mock?'给知识一次实战机会。':'用练习，检验理解。'}</h2>
    <p className="muted">题目来自本次生成。完成作答后可查看参考答案与解析。</p>
    {questions.map((question,i)=><section className="exam-question" key={i}>
      <h3>{String(i+1).padStart(2,'0')} / {question.type||question.question_type}</h3>
      <p className="question-stem">{question.question}</p>
      {!!question.options?.length&&<ul>{question.options.map((option,j)=><li key={j}>{option}</li>)}</ul>}
      <textarea aria-label={`第 ${i+1} 题答案`} placeholder="写下你的答案与思路…" value={answers[i]||''} onChange={event=>setAnswers(old=>({...old,[i]:event.target.value}))}/>
      <Button variant="text" aria-expanded={!!revealed[i]} title="查看解析后，可聚焦此按钮按 Alt+1–4 自评：不会、困难、基本掌握、熟练" onClick={()=>void reveal(i)} onKeyDown={event=>{if(event.altKey&&!event.repeat)void selfAssess(i,event.key);}}>{revealed[i]?'收起解析':'查看参考答案'}</Button>
      {revealed[i]&&<div className="concept-callout"><div>
        <strong>参考答案</strong><p>{question.answer||'本题未返回答案'}</p>
        <strong>解析</strong><p>{question.explanation||'本题未返回详细解析'}
          {assessments[i]&&` · ${assessments[i].assessment.score===null?'需结合 Rubric 自评（解析按钮 Alt+1–4）':`本题得分：${Math.round(assessments[i].assessment.score!*100)}%`} · ${assessments[i].assessment.uncertainty}`}
          {!!assessments[i]?.assessment.missing_points.length&&` · 待核对要点：${assessments[i].assessment.missing_points.join('；')}`}
        </p>
      </div></div>}
      <p className="source-label">{question.source_basis||question.source_hint||'基于上传材料生成'}{question.concept&&` · ${question.concept}`}{learning.error&&` · ${learning.error}`}</p>
    </section>)}
  </>;
}
