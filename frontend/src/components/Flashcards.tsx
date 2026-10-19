import {useState} from 'react';
import type {AnkiCard} from '../api/client';
import {Button} from '../design-system/Button';
import {Icon} from '../design-system/Icon';
import {useLearningProgress} from '../hooks/useLearningProgress';
export function Flashcards({cards,jobId}: {cards:AnkiCard[];jobId?:string}) {
  const [index,setIndex]=useState(0);const [flipped,setFlipped]=useState(false);
  const learning=useLearningProgress(jobId);
  function flip(){setFlipped(!flipped);if(!flipped)void learning.record(cards[index].card_id||'','recall','',0);}
  function rate(key:string){if(flipped&&['1','2','3','4'].includes(key))void learning.record(cards[index].card_id||'','recall','',0,(Number(key)-1)/3);}
  if(!cards.length)return <div className="empty-projects"><h2>这份资料尚未生成卡片</h2><p>可以补充资料，选择知识卡片模式后重新生成。</p></div>;
  const card=cards[index];
  const rating=learning.progress?.attempts.filter(attempt=>attempt.kind==='recall'&&attempt.item_id===card.card_id&&attempt.assessment.score!==null).slice(-1)[0];
  const due=(card.knowledge_ids||[]).map(id=>learning.progress?.mastery[id]?.next_review).filter((value):value is string=>!!value).sort()[0];
  const status=rating?`上次自评 ${Math.round(rating.assessment.score!*100)}%${due?` · 复习日期 ${due.slice(0,10)}`:''}`:'';
  function move(next:number){setIndex(next);setFlipped(false);}
  return <><div className="document-label">ACTIVE RECALL<span>{index+1} / {cards.length}</span></div><h2>想一想，再看答案。</h2><button type="button" className={`flashcard ${flipped?'flipped':''}`} aria-label={flipped?'查看问题':'查看答案'} aria-pressed={flipped} onClick={flip} onKeyDown={event=>rate(event.key)}><span>{flipped?'ANSWER / 答案':'QUESTION / 问题'}</span><Icon name="cards" size={30}/><strong>{flipped?card.back:card.front}</strong><small>{flipped&&card.card_id?'按 1–4 自评：不会、困难、基本掌握、熟练':`点击卡片${flipped?'返回问题':'查看答案'}`}{status&&` · ${status}`}</small></button>{card.source_hint&&<p className="source-label">来源：{card.source_hint}{learning.error&&` · ${learning.error}`}</p>}<div className="card-navigation"><Button variant="secondary" disabled={index===0} onClick={()=>move(index-1)}>上一张</Button><span>{index+1} / {cards.length}</span><Button variant="secondary" disabled={index===cards.length-1} onClick={()=>move(index+1)}>下一张</Button></div></>;
}
