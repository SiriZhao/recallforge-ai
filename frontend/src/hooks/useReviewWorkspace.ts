import {useCallback, useEffect, useRef, useState} from 'react';
import {createGenerateReviewJob, ensureReviewWorkspace, getGenerateReviewJob, reviewApi, reviewUpload, type GenerateReviewJob, type GenerateReviewResponse, type LLMConfig} from '../api/client';
import {DEFAULT_FORM, MODES, listProjects, userFacingError, type Diagnosis, type ErrorContext, type JobReference, type Material, type Mode, type Page, type Project, type ProjectForm, type UserFacingError} from '../api/workspace';

export function readModel(): LLMConfig {
  const fallback: LLMConfig = {provider:'deepseek',base_url:'https://api.deepseek.com',model:'',api_key:'',enabled:false};
  try {
    const value: unknown = JSON.parse(localStorage.getItem('examforge-byok') || 'null');
    if (!value || typeof value !== 'object') return fallback;
    const config = value as Record<string, unknown>;
    return {...fallback, ...Object.fromEntries(['provider','base_url','model','api_key'].filter(key => typeof config[key] === 'string').map(key => [key,config[key]]))};
  } catch { return fallback; }
}
function readJobs(): Record<string, JobReference> {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem('recallforge-jobs-' + localStorage.getItem('examforge-workspace-id')) || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    const valid = new Set(['pending','parsing','ocr','building_evidence','llm','validating','completed','partial','retrying','retryable_failed','failed','queued','running']);
    return Object.fromEntries(Object.entries(parsed).filter(([,value]) => value && typeof value.id === 'string' && valid.has(value.status)));
  } catch {return {};}
}
export function useReviewWorkspace() {
  const [page,setPage] = useState<Page>('home');
  const [projects,setProjects] = useState<Project[]>([]);
  const [project,setProject] = useState<Project | null>(null);
  const [files,setFiles] = useState<File[]>([]);
  const [materials,setMaterials] = useState<Material[]>([]);
  const [form,setForm] = useState<ProjectForm>({...DEFAULT_FORM});
  const [mode,setMode] = useState<Mode>('cram');
  const [diagnosis,setDiagnosis] = useState<Diagnosis | null>(null);
  const [job,setJob] = useState<GenerateReviewJob | null>(null);
  const [jobId,setJobId] = useState<string | null>(null);
  const [result,setResult] = useState<GenerateReviewResponse | null>(null);
  const [jobs,setJobs] = useState<Record<string,JobReference>>({});
  const [llm,setLlm] = useState<LLMConfig>(readModel);
  const [error,setError] = useState<UserFacingError | null>(null);
  const [busy,setBusy] = useState('');
  const [ready,setReady] = useState(false);
  const [pollError,setPollError] = useState<UserFacingError | null>(null);
  const [pollAttempt,setPollAttempt] = useState(0);
  const [recovery,setRecovery] = useState<{label:string;run:()=>void} | null>(null);
  const lock = useRef(false); const mounted = useRef(true); const initialized = useRef(false);
  useEffect(() => {mounted.current=true;return () => {mounted.current=false;};},[]);
  const initialize = useCallback(async () => {
    setError(null);setRecovery(null);setBusy('正在连接工作空间');
    try {await ensureReviewWorkspace(); const items = await listProjects(); if (!mounted.current) return;setProjects(items);setJobs(readJobs());setReady(true);}
    catch (e) {if (mounted.current) setError(userFacingError(e,'workspace'));}
    finally {if (mounted.current) setBusy('');}
  },[]);
  useEffect(() => {if (!initialized.current) {initialized.current=true;void initialize();}},[initialize]);
  useEffect(() => {
    if (!ready) return;
    try {localStorage.setItem('recallforge-jobs-' + localStorage.getItem('examforge-workspace-id'),JSON.stringify(jobs));}
    catch { /* Job retrieval remains available for this session. */ }
  },[jobs,ready]);
  const remember = useCallback((projectId: string, reference: JobReference) => {
    setJobs(current => ({...current,[projectId]:reference}));
  },[]);
  useEffect(() => {
    if (!jobId || !project) return;
    const controller = new AbortController(); let timer: number | undefined;
    setPollError(null);
    async function poll() {
      try {
        const current = await getGenerateReviewJob(jobId!,controller.signal);
        if (controller.signal.aborted) return;
        setJob(current);remember(project!.id,{id:current.job_id,status:current.status});
        if (current.status === 'completed' || current.status === 'partial') {
          if (!current.result) {setPollError({kind:'service',title:'结果还没有准备好',message:'任务已经完成，但结果暂时没有返回。请重新查询进度。'});return;}
          setResult(current.result);return;
        }
        if (current.status === 'failed' || current.status === 'retryable_failed') return;
        timer=window.setTimeout(() => void poll(),1200);
      } catch (e) {if (!controller.signal.aborted) setPollError(userFacingError(e,'generation'));}
    }
    void poll();
    return () => {controller.abort();window.clearTimeout(timer);};
  },[jobId,project?.id,pollAttempt,remember]);

  async function action<T>(label: string, context: ErrorContext, work: () => Promise<T>, retry?: () => void): Promise<T | undefined> {
    if (lock.current) return;
    lock.current=true;setBusy(label);setError(null);setRecovery(null);
    try {return await work();} catch(e) {if (mounted.current) {setError(userFacingError(e,context));if(retry)setRecovery({label:'重试',run:retry});}}
    finally {lock.current=false;if (mounted.current) setBusy('');}
  }
  function newProject() {
    if (lock.current) return;
    setJobId(null);setProject(null);setJob(null);setResult(null);setMaterials([]);setFiles([]);setForm({...DEFAULT_FORM});setDiagnosis(null);setPage('home');setError(null);setPollError(null);setRecovery(null);
  }
  async function upload() {
    if (!ready || !files.length) return;
    await action('正在上传并解析资料','upload',async () => {
      let current = project;
      if (!current) {
        current=await reviewApi<Project>('/projects','POST',{...form,course_name:form.course_name.trim() || files[0].name.replace(/\.[^.]+$/,'')});
        if (!mounted.current) return;
        setProject(current);setProjects(old => [current!,...old]);setForm(old => ({...old,course_name:current!.course_name}));
      }
      const response = await reviewUpload<{files: Material[]}>(`/projects/${current.id}/upload`,files);
      if (!mounted.current) return;
      const combined = [...materials,...response.files];setMaterials(combined);setFiles([]);setDiagnosis(null);setResult(null);setJobId(null);setJob(null);
      setJobs(old => {const next={...old};delete next[current!.id];return next;});
      setProjects(old => old.map(p => p.id === current!.id ? {...p,files:combined} : p));setPage('materials');
    },()=>void upload());
  }
  async function diagnose() {
    if (!project || !materials.length) return;
    await action('正在生成资料诊断','diagnosis',async () => {
      await Promise.all(materials.map(file => reviewApi(`/projects/${project.id}/files/${file.id}`,'PATCH',{role:file.role})));
      const response=await reviewApi<Diagnosis>(`/projects/${project.id}/diagnosis`);
      if (mounted.current) {setDiagnosis(response);setPage('diagnosis');}
    },()=>void diagnose());
  }
  async function generate() {
    if (!project || !materials.length || !diagnosis || lock.current) return;
    await action('正在创建生成任务','generation',async () => {
      const selected=MODES.find(m => m.id === mode)!;
      const created=await createGenerateReviewJob({
        files:materials.map(f => f.saved_filename), project_id:project.id, title:project.course_name, course_name:project.course_name,
        exam_type:form.exam_type,study_goal:selected.goal,output_style:selected.style,detail_level:'detailed',
        exam_date:form.exam_date,daily_minutes:form.daily_minutes,mastery_level:form.mastery_level,
        target_score:form.target_score,focus:form.focus,learning_mode:mode,
        material_types:Object.fromEntries(materials.map(file=>[file.saved_filename,file.role])),
        export_format:'md',enable_chunked_llm:true,retry_on_context_too_long:true,
        ocr_config:{provider:'rapidocr',mode:'fast',language:'chi_sim+eng'},
        llm_config:{...llm,enabled:Boolean(llm.api_key?.trim())},
      });
      if (!mounted.current) return;
      setResult(null);setJob(null);remember(project.id,{id:created.job_id,status:'pending'});setJobId(created.job_id);setPage('processing');
    },()=>void generate());
  }
  async function openProject(item: Project) {
    if (lock.current) return;
    await action('正在打开项目','project',async () => {
      const items=await listProjects();const fresh=items.find(p => p.id===item.id);
      if (!fresh) throw new Error('项目不存在，请刷新项目列表。');
      if (!mounted.current) return;
      setProjects(items);setProject(fresh);setMaterials(fresh.files || []);setFiles([]);setDiagnosis(null);setResult(null);setJob(null);
      const knownExam = ['unknown','closed_book','open_book','computer_based','programming','lab_exam','essay_based','oral_presentation','coursework_report'];
      setForm({...DEFAULT_FORM,...fresh,exam_type:knownExam.includes(fresh.exam_type) ? fresh.exam_type as ProjectForm['exam_type'] : 'unknown'});
      const reference=jobs[item.id];setJobId(reference?.id || null);setPollAttempt(attempt=>attempt+1);setPage(reference ? 'processing' : 'materials');
    },()=>void openProject(item));
  }
  async function deleteProject(item: Project): Promise<boolean> {
    const deleted=await action('正在删除项目','project',async () => {
      await reviewApi<void>(`/projects/${item.id}`,'DELETE');
      if (!mounted.current) return false;
      setProjects(current=>current.filter(projectItem=>projectItem.id!==item.id));
      setJobs(current=>{const next={...current};delete next[item.id];return next;});
      if(project?.id===item.id){setProject(null);setMaterials([]);setFiles([]);setDiagnosis(null);setJobId(null);setJob(null);setResult(null);setForm({...DEFAULT_FORM});}
      return true;
    },()=>void deleteProject(item));
    return deleted===true;
  }
  return {page,setPage,projects,project,files,setFiles,materials,setMaterials,form,setForm,mode,setMode,diagnosis,job,result,
    jobs,llm,setLlm,error,busy,ready,pollError,recovery,initialize,newProject,upload,diagnose,generate,openProject,deleteProject,
    retryPoll: () => setPollAttempt(attempt=>attempt+1)};
}
export type ReviewWorkspace = ReturnType<typeof useReviewWorkspace>;
