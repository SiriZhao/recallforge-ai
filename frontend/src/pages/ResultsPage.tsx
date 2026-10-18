import {useState} from 'react';
import {downloadFilename,type ExportFormat,type GenerateReviewResponse} from '../api/client';
import {downloadArtifact,userFacingError,type UserFacingError} from '../api/workspace';
import {Button} from '../design-system/Button';
import {Icon} from '../design-system/Icon';
import {MarkdownView} from '../components/MarkdownView';
import {RESULT_TABS,ResultTabs} from '../components/ResultTabs';
import {Flashcards} from '../components/Flashcards';
import {Questions} from '../components/Questions';
import {KnowledgeMap} from '../components/KnowledgeMap';

export function ResultsPage({result,jobId,onSettings}: {result:GenerateReviewResponse;jobId?:string;onSettings:()=>void}) {
  const [tab,setTab]=useState(0);const [exporting,setExporting]=useState('');const [error,setError]=useState<UserFacingError|null>(null);
  const report=result.review_report;
  async function download(path:string,format:string) {
    if(exporting)return;setExporting(format);setError(null);
    try {await downloadArtifact(jobId ? `/api/review/jobs/${encodeURIComponent(jobId)}/download/${format === 'csv' ? 'anki' : format}` : path,downloadFilename(path)||`${report.title}.${format}`);}
    catch(e){setError(userFacingError(e,'download'));}finally{setExporting('');}
  }
  const source=result.llm_status==='success'?'AI 深度整理':result.llm_status==='partial'?'AI 深度整理 · 部分证据待重试':result.llm_status==='failed'?'基础离线整理 · AI 调用失败':'基础离线整理';
  return <section className="results-page"><div className="result-heading"><div><div className="eyebrow">YOUR KNOWLEDGE, CONNECTED</div><h1>{report.title}</h1><p className="muted">生成于 {report.generated_at} · {source}</p></div><div className="download-actions">{(['md','docx','pdf'] as ExportFormat[]).map(format=>{
    const path=result.download_links[format] || (format===result.export_format?result.download_path:undefined);
    const available=Boolean(jobId||path);
    return <Button key={format} variant={format==='md'?'primary':'secondary'} title={!available?'本次生成未提供该格式':undefined} disabled={!available||!!exporting} loading={exporting===format} onClick={()=>available&&void download(path||'',format)}><Icon name="download" size={16}/>{format==='md'?'Markdown':format==='docx'?'Word':'PDF'}</Button>;
  })}{(jobId||result.anki_csv_download_path)&&<Button variant="secondary" disabled={!!exporting} loading={exporting==='csv'} onClick={()=>void download(result.anki_csv_download_path||'','csv')}>Anki CSV</Button>}</div></div>
    {error&&<div className="error recovery-panel" role="alert"><strong>{error.title}</strong><p>{error.message}</p><Button variant="secondary" disabled={!!exporting} onClick={()=>setError(null)}>我知道了</Button></div>}
    {(result.llm_status==='failed'||result.fallback_used)&&<div className="notice warning"><strong>本次结果使用了回退整理</strong><p>{result.llm_error?.message||'请核对生成内容。'} {result.llm_error?.suggestion}</p><Button variant="text" onClick={onSettings}>检查模型配置</Button></div>}
    <ResultTabs value={tab} onChange={setTab}/><div className="result-layout"><article className="result-content" id="result-panel" role="tabpanel" aria-labelledby={`result-tab-${tab}`} tabIndex={0}>
      {tab===0&&<><div className="document-label">LEARNING SUMMARY</div>{result.markdown?<MarkdownView markdown={result.markdown}/>:<><h2>{report.title}</h2><p>{report.summary}</p></>}
        <details className="report-details"><summary>复习计划与资料依据</summary><h3>推荐复习顺序</h3><ol>{report.review_order.map((item,i)=><li key={i}><strong>{item.chapter}</strong>：{item.reason}</li>)}</ol>{report.sprint_plans.map((plan,i)=><section key={i}><h3>{plan.title}</h3><ul>{plan.schedule.map((item,j)=><li key={j}>{item}</li>)}</ul></section>)}<h3>高频考点</h3><ul>{report.high_frequency_points.map((point,i)=><li key={i}>{point}</li>)}</ul><h3>往年题分析</h3><p>{report.past_exam_analysis.summary}</p>{report.past_exam_analysis.high_frequency_topics.map((topic,i)=><p key={i}>{topic.topic} · {topic.chapter} · {topic.frequency} 次 · {topic.question_types.join('、')}</p>)}{report.question_types?.map((type,i)=><section key={i}><h3>{type.name}</h3><p>{type.evidence}</p><p>{type.answer_strategy}</p></section>)}</details>
        <details className="report-details"><summary>生成质量与资料缺口</summary>{report.quality&&<><p>质量评分：{report.quality.quality_score} / 100</p>{[...report.quality.quality_warnings,...report.quality.quality_failures].map((item,i)=><p key={i}>{item}</p>)}</>}{report.insufficient_materials.map((item,i)=><p key={i}>{item}</p>)}{result.generation_summary&&<p>处理 {result.generation_summary.files_processed} 份文件，共 {result.generation_summary.pages_total} 页；OCR 识别 {result.generation_summary.pages_ocr_processed} 页。</p>}<p>生成内容用于辅助学习，请对照原始材料核对。</p></details>
      </>}
      {tab===1&&<KnowledgeMap report={report}/>}
      {tab===2&&<Flashcards cards={report.anki_cards}/>}
      {tab===3&&<Questions key="practice" questions={report.practice_questions ?? report.mock_exam.questions} mock={false}/>}
      {tab===4&&<Questions key="mock" questions={report.mock_exam.questions} mock/>}
    </article><aside className="result-aside"><Icon name="book" size={24}/><h3>本章学习路径</h3><p>理解概念，连接知识，<br/>再通过回忆和练习巩固。</p>{RESULT_TABS.map((label,i)=><button key={label} className={tab===i?'active':''} onClick={()=>setTab(i)}><span>0{i+1}</span>{label}<Icon name="arrow" size={14}/></button>)}<small>导出包含本次完整复习报告。<br/>练习题与模拟卷使用本次生成的题库。</small></aside></div></section>;
}
