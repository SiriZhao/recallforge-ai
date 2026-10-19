import {useEffect,useRef,useState} from 'react';
import {reviewApi} from '../api/client';
export type Assessment={score:number|null;assessed_by:string;needs_self_review:boolean;missing_points:string[];suggestion:string;uncertainty:string};
export type LearningAttempt={item_id:string;kind:string;answer:string;assessment:Assessment;created_at:string};
export type LearningProgress={attempts:LearningAttempt[];mastery:Record<string,unknown>;recommendation:{items:{knowledge_id:string;name:string;estimated_minutes:number;mastery:number}[]}};

export function useLearningProgress(jobId?:string) {
  const [progress,setProgress]=useState<LearningProgress|null>(null);
  const [error,setError]=useState('');
  const mounted=useRef(true);
  const session=useRef(crypto.randomUUID());
  const pending=useRef(new Map<string,{item_id:string;kind:string;answer:string;nonce:string;duration_seconds:number;self_score?:number;exam_session_id?:string}>());
  const loadSequence=useRef(0);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  useEffect(()=>{
    if(!jobId)return;
    let active=true;
    const load=()=>{const sequence=++loadSequence.current;void reviewApi<LearningProgress>(`/learning/jobs/${encodeURIComponent(jobId)}/progress`).then(value=>{if(active&&sequence===loadSequence.current&&Array.isArray(value?.attempts)&&value?.recommendation?.items)setProgress(value);}).catch(()=>{/* Legacy jobs remain usable without assessment storage. */});};
    load();window.addEventListener('recallforge-learning-updated',load);
    return()=>{active=false;window.removeEventListener('recallforge-learning-updated',load);};
  },[jobId]);
  async function record(itemId:string,kind:'practice'|'exam'|'recall',answer:string,duration:number,selfScore?:number):Promise<LearningAttempt|undefined>{
    if(!jobId||!itemId)return;
    const signature=JSON.stringify([jobId,itemId,kind,answer,selfScore]);
    const payload=pending.current.get(signature)||{item_id:itemId,kind,answer,nonce:crypto.randomUUID(),duration_seconds:Math.max(0,Math.min(86400,duration)),self_score:selfScore,exam_session_id:kind==='exam'?session.current:undefined};
    pending.current.set(signature,payload);
    setError('');
    try{
      const result=await reviewApi<LearningAttempt>(`/learning/jobs/${encodeURIComponent(jobId)}/attempts`,'POST',payload);
      pending.current.delete(signature);
      window.dispatchEvent(new Event('recallforge-learning-updated'));
      return result;
    }catch{if(mounted.current)setError('作答保存状态尚未确认，请重试；参考答案仍可查看。');}
  }
  return {progress,record,error};
}
