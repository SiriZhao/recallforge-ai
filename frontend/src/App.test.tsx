import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import App from './App';
import {createGenerateReviewJob,ensureReviewWorkspace,getGenerateReviewJob,reviewApi,reviewUpload} from './api/client';
import {completedJob,diagnosis,material,project} from './test/fixtures';
vi.mock('./api/client',async()=>{
  const actual=await vi.importActual<typeof import('./api/client')>('./api/client');
  return {...actual,ensureReviewWorkspace:vi.fn(),reviewApi:vi.fn(),reviewUpload:vi.fn(),createGenerateReviewJob:vi.fn(),getGenerateReviewJob:vi.fn()};
});
beforeEach(()=>{
  localStorage.clear();vi.clearAllMocks();
  localStorage.setItem('recallforge-welcome-complete-v3.1','1');
  vi.mocked(ensureReviewWorkspace).mockResolvedValue();
  vi.mocked(reviewApi).mockImplementation(async(path,method)=>{
    if(path==='/projects')return method==='POST'?project:[] as never;
    if(path.endsWith('/diagnosis'))return diagnosis as never;
    return {} as never;
  });
  vi.mocked(reviewUpload).mockResolvedValue({files:[material]});
  vi.mocked(createGenerateReviewJob).mockResolvedValue({job_id:'job1'});
  vi.mocked(getGenerateReviewJob).mockResolvedValue(completedJob);
});
afterEach(()=>{cleanup();vi.unstubAllGlobals();vi.restoreAllMocks();});
async function selectAndUpload() {
  render(<App/>);
  await waitFor(()=>expect(screen.getByRole('button',{name:'选择文件'})).toBeEnabled());
  const file=new File(['pdf content'],'高等数学.pdf',{type:'application/pdf'});
  fireEvent.change(screen.getByLabelText('选择学习资料'),{target:{files:[file]}});
  fireEvent.click(screen.getByRole('button',{name:'上传 1 份资料'}));
  await screen.findByRole('button',{name:'生成资料诊断'});
}
async function generate() {
  await selectAndUpload();
  fireEvent.change(screen.getByLabelText('资料用途：高等数学.pdf'),{target:{value:'past_exam'}});
  fireEvent.click(screen.getByRole('button',{name:'生成资料诊断'}));
  await screen.findByText('先理清资料，再开始学习。');
  fireEvent.click(screen.getByRole('button',{name:'确认并开始生成'}));
}
describe('RecallForge product workflow',()=>{
  it('guides first-time students through three steps and only shows once',async()=>{
    localStorage.removeItem('recallforge-welcome-complete-v3.1');render(<App/>);
    const dialog=screen.getByRole('dialog',{name:'欢迎使用 RecallForge'});
    expect(within(dialog).getByText('上传文件')).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button',{name:'下一步'}));
    expect(within(dialog).getByText('AI 理解内容')).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button',{name:'下一步'}));
    expect(within(dialog).getByText('生成复习资料')).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button',{name:'开始使用'}));
    expect(localStorage.getItem('recallforge-welcome-complete-v3.1')).toBe('1');
    expect(screen.queryByRole('dialog',{name:'欢迎使用 RecallForge'})).not.toBeInTheDocument();
  });
  it('starts at the real upload dashboard without sample projects',async()=>{
    render(<App/>);
    await waitFor(()=>expect(screen.getByRole('button',{name:'选择文件'})).toBeEnabled());
    expect(screen.getByRole('heading',{name:'上传你的学习资料'})).toBeInTheDocument();
    expect(screen.getByText('第一份知识，等你开启')).toBeInTheDocument();
    expect(screen.queryByText('演示模型')).not.toBeInTheDocument();
    expect(screen.queryByText('我的学习空间')).not.toBeInTheDocument();
    expect(screen.queryByText('PERSONAL WORKSPACE')).not.toBeInTheDocument();
    expect(screen.queryByText('学习者')).not.toBeInTheDocument();
    expect(screen.queryByText('个人空间')).not.toBeInTheDocument();
    expect(screen.getByRole('button',{name:'新建学习项目'})).toBeInTheDocument();
  });
  it('preserves upload, role diagnosis, generation and real report exports',async()=>{
    await generate();
    await screen.findByRole('button',{name:'查看学习内容'});
    expect(reviewUpload).toHaveBeenCalledWith('/projects/p1/upload',[expect.any(File)]);
    expect(reviewApi).toHaveBeenCalledWith('/projects/p1/files/f1','PATCH',{role:'past_exam'});
    expect(createGenerateReviewJob).toHaveBeenCalledWith(expect.objectContaining({files:['saved.pdf'],export_format:'md',study_goal:'one_day_sprint',
      exam_date:'',daily_minutes:90,mastery_level:'forgotten',target_score:'',focus:'',learning_mode:'cram',material_types:{'saved.pdf':'past_exam'}}));
    expect(vi.mocked(createGenerateReviewJob).mock.calls[0][0]).not.toHaveProperty('export_formats');
    fireEvent.click(screen.getByRole('button',{name:'查看学习内容'}));
    expect(screen.getByText('真实接口返回的总结')).toBeInTheDocument();
    expect(screen.getByRole('button',{name:'Word'})).toBeEnabled();
    expect(screen.getByRole('button',{name:'Anki CSV'})).toBeEnabled();
    fireEvent.click(screen.getByRole('tab',{name:'问答卡片'}));
    fireEvent.click(screen.getByRole('button',{name:'查看答案'}));
    expect(screen.getByText('连续累积量')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button',{name:'下一张'}));
    expect(screen.getByText('基本定理？')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab',{name:'模拟考试'}));
    fireEvent.change(screen.getByLabelText('第 1 题答案'),{target:{value:'1'}});
    fireEvent.click(screen.getByRole('button',{name:'查看参考答案'}));
    expect(screen.getByText('答案为 1')).toBeInTheDocument();
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,blob:async()=>new Blob(['report'])}));
    const create=vi.fn().mockReturnValue('blob:report');const revoke=vi.fn();
    Object.defineProperty(URL,'createObjectURL',{value:create,configurable:true});Object.defineProperty(URL,'revokeObjectURL',{value:revoke,configurable:true});
    const click=vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(()=>{});
    fireEvent.click(screen.getByRole('button',{name:'PDF'}));
    await waitFor(()=>expect(click).toHaveBeenCalled());
    expect(fetch).toHaveBeenCalledWith('/api/review/jobs/job1/download/pdf');
  });
  it('keeps selected files after upload failure and allows retry without duplicate project creation',async()=>{
    vi.mocked(reviewUpload).mockRejectedValueOnce(new Error('上传失败'));
    await selectAndUploadFailure();
    expect(screen.getByRole('alert')).toHaveTextContent('这份文件暂时无法处理');
    expect(screen.getByRole('button',{name:'重试'})).toBeEnabled();
    fireEvent.click(screen.getByRole('button',{name:'上传 1 份资料'}));
    await screen.findByRole('button',{name:'生成资料诊断'});
    expect(vi.mocked(reviewApi).mock.calls.filter(([path,method])=>path==='/projects'&&method==='POST')).toHaveLength(1);
  });
  it('shows failed generation and retry without rendering a fake result',async()=>{
    vi.mocked(getGenerateReviewJob).mockResolvedValue({...completedJob,status:'retryable_failed',result:null,error:'模型服务不可用',error_code:'LLM_PROVIDER_ERROR',retryable:true});
    await generate();
    expect(await screen.findByText('模型暂时无法响应')).toBeInTheDocument();
    expect(screen.getByText('AI 分章生成').closest('li')).toHaveTextContent('失败');
    expect(screen.getByText('保存学习内容').closest('li')).toHaveTextContent('等待中');
    expect(screen.getByRole('status')).toHaveTextContent('生成失败 · 已停止');
    expect(screen.queryByText('处理中')).not.toBeInTheDocument();
    expect(screen.getByRole('button',{name:'从 checkpoint 继续'})).toBeEnabled();
    expect(screen.queryByRole('button',{name:'查看学习内容'})).not.toBeInTheDocument();
  });
  it('retries interrupted polling without creating another generation task',async()=>{
    vi.mocked(getGenerateReviewJob).mockRejectedValueOnce(new Error('网络中断')).mockResolvedValue(completedJob);
    await generate();
    fireEvent.click(await screen.findByRole('button',{name:'重新查询进度'}));
    await screen.findByRole('button',{name:'查看学习内容'});
    expect(createGenerateReviewJob).toHaveBeenCalledTimes(1);
  });
  it('reports unavailable exports instead of silently failing',async()=>{
    await generate();fireEvent.click(await screen.findByRole('button',{name:'查看学习内容'}));
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:false,status:404}));
    fireEvent.click(screen.getByRole('button',{name:'PDF'}));
    expect(await screen.findByRole('alert')).toHaveTextContent('导出文件创建失败');
    expect(screen.getByRole('alert')).toHaveTextContent('无需重新生成');
  });
  it('saves model settings without claiming a verified connection',async()=>{
    render(<App/>);fireEvent.click(screen.getByRole('button',{name:'打开设置'}));
    const modal=screen.getByRole('dialog');
    fireEvent.change(within(modal).getByLabelText('API Key'),{target:{value:'test-key'}});
    fireEvent.click(within(modal).getByRole('button',{name:'保存配置'}));
    expect(JSON.parse(localStorage.getItem('examforge-byok')!).api_key).toBe('test-key');
    expect(screen.getByText('AI 深度整理')).toBeInTheDocument();
  });
  it('retries workspace initialization and does not enable upload early',async()=>{
    vi.mocked(ensureReviewWorkspace).mockRejectedValueOnce(new Error('后端不可用')).mockResolvedValue();
    render(<App/>);expect(screen.getByRole('button',{name:'选择文件'})).toBeDisabled();
    fireEvent.click(await screen.findByRole('button',{name:'重新连接'}));
    await waitFor(()=>expect(screen.getByRole('button',{name:'选择文件'})).toBeEnabled());
  });
  it('requires confirmation before deleting a project and removes it from history',async()=>{
    vi.mocked(reviewApi).mockImplementation(async(path,method)=>{
      if(path==='/projects'&&method!=='POST')return [project] as never;
      if(path==='/projects/p1'&&method==='DELETE')return undefined as never;
      return {} as never;
    });
    render(<App/>);await waitFor(()=>expect(screen.getByRole('button',{name:'我的项目'})).toBeEnabled());
    fireEvent.click(screen.getByRole('button',{name:'我的项目'}));
    fireEvent.click(await screen.findByRole('button',{name:'删除 高等数学'}));
    const dialog=screen.getByRole('dialog',{name:'删除这个项目？'});
    expect(within(dialog).getByText(/无法撤销/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button',{name:'确认删除'}));
    await waitFor(()=>expect(reviewApi).toHaveBeenCalledWith('/projects/p1','DELETE'));
    expect(await screen.findByText('第一份知识，等你开启')).toBeInTheDocument();
  });
});
async function selectAndUploadFailure(){
  render(<App/>);await waitFor(()=>expect(screen.getByRole('button',{name:'选择文件'})).toBeEnabled());
  fireEvent.change(screen.getByLabelText('选择学习资料'),{target:{files:[new File(['pdf'],'高等数学.pdf',{type:'application/pdf'})]}});
  fireEvent.click(screen.getByRole('button',{name:'上传 1 份资料'}));
  await screen.findByRole('alert');
}
