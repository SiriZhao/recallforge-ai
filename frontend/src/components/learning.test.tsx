import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {Questions} from './Questions';
import {Flashcards} from './Flashcards';
import {reviewApi} from '../api/client';
vi.mock('../api/client',()=>({reviewApi:vi.fn()}));
const assessment={score:1,assessed_by:'deterministic_choice',needs_self_review:false,missing_points:[],suggestion:'Review',uncertainty:'Reference-relative score'};
const question={question_id:'q1',question_type:'multiple_choice',question:'Which option?',options:['A. First','B. Second'],answer:'A',chapter:'Chapter',concept:'Concept',explanation:'First matches the source'};
beforeEach(()=>{vi.clearAllMocks();vi.mocked(reviewApi).mockImplementation(async(_path,method)=>method==='POST'?{item_id:'q1',kind:'practice',answer:'A',assessment,created_at:'2030-01-01'} as never:{attempts:[],mastery:{},recommendation:{items:[]}} as never);});
afterEach(cleanup);

it('persists real practice input and binds returned assessment within existing markup',async()=>{
  const {container}=render(<Questions questions={[question]} mock={false} jobId="owned-job"/>);
  fireEvent.change(screen.getByLabelText('第 1 题答案'),{target:{value:'A'}});
  fireEvent.click(screen.getByRole('button',{name:'查看参考答案'}));
  await waitFor(()=>expect(reviewApi).toHaveBeenCalledWith('/learning/jobs/owned-job/attempts','POST',expect.objectContaining({item_id:'q1',kind:'practice',answer:'A'})));
  expect(await screen.findByText(/本题得分：100%/)).toBeInTheDocument();
  expect(container.querySelectorAll('.exam-question')).toHaveLength(1);
  expect(container.querySelectorAll('.concept-callout > div')).toHaveLength(1);
  expect(container.querySelectorAll('textarea')).toHaveLength(1);
});

it('restores persisted answers without changing practice/exam mode',async()=>{
  vi.mocked(reviewApi).mockResolvedValue({attempts:[{item_id:'q1',kind:'practice',answer:'saved answer',assessment,created_at:'2030-01-01'}],mastery:{},recommendation:{items:[]}} as never);
  render(<Questions questions={[question]} mock={false} jobId="owned-job"/>);
  await waitFor(()=>expect(screen.getByLabelText('第 1 题答案')).toHaveValue('saved answer'));
});

it('records recall exposure separately from an explicit learner rating',async()=>{
  render(<Flashcards cards={[{card_id:'c1',front:'Recall question',back:'Recall answer',tags:'test'}]} jobId="owned-job"/>);
  fireEvent.click(screen.getByRole('button',{name:'查看答案'}));
  await waitFor(()=>expect(reviewApi).toHaveBeenCalledWith('/learning/jobs/owned-job/attempts','POST',expect.objectContaining({kind:'recall',self_score:undefined})));
  fireEvent.keyDown(screen.getByRole('button',{name:'查看问题'}),{key:'4'});
  await waitFor(()=>expect(reviewApi).toHaveBeenCalledWith('/learning/jobs/owned-job/attempts','POST',expect.objectContaining({kind:'recall',self_score:1})));
});

it('reuses the idempotency token after an uncertain network outcome',async()=>{
  let attempts=0;
  vi.mocked(reviewApi).mockImplementation(async(_path,method)=>{
    if(method==='POST'){
      if(++attempts===1)throw new Error('connection interrupted after submission');
      return {item_id:'q1',kind:'practice',answer:'A',assessment,created_at:'2030-01-01'} as never;
    }
    return {attempts:[],mastery:{},recommendation:{items:[]}} as never;
  });
  render(<Questions questions={[question]} mock={false} jobId="owned-job"/>);
  fireEvent.change(screen.getByLabelText('第 1 题答案'),{target:{value:'A'}});
  fireEvent.click(screen.getByRole('button',{name:'查看参考答案'}));
  expect(await screen.findByText(/保存状态尚未确认/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'收起解析'}));
  fireEvent.click(screen.getByRole('button',{name:'查看参考答案'}));
  await waitFor(()=>expect(attempts).toBe(2));
  const posts=vi.mocked(reviewApi).mock.calls.filter(([,method])=>method==='POST');
  expect(posts[0][2]).toEqual(posts[1][2]);
});

it('binds persisted recall rating and due date without adding layout elements',async()=>{
  vi.mocked(reviewApi).mockResolvedValue({attempts:[{item_id:'c1',kind:'recall',answer:'',assessment:{...assessment,assessed_by:'learner_self_report'},created_at:'2030-01-01'}],
    mastery:{k1:{mastery:.6,next_review:'2030-01-03T00:00:00Z'}},recommendation:{items:[]}} as never);
  const {container}=render(<Flashcards cards={[{card_id:'c1',knowledge_ids:['k1'],front:'Recall question',back:'Recall answer',tags:'test'}]} jobId="owned-job"/>);
  expect(await screen.findByText(/上次自评 100% · 复习日期 2030-01-03/)).toBeInTheDocument();
  expect(container.querySelectorAll('.flashcard > small')).toHaveLength(1);
});

it('does not carry a saved answer into a different generation job',async()=>{
  vi.mocked(reviewApi).mockImplementation(async(path)=>path.includes('first-job')?{attempts:[{item_id:'q1',kind:'practice',answer:'old private answer',assessment,created_at:'2030-01-01'}],mastery:{},recommendation:{items:[]}} as never:{attempts:[],mastery:{},recommendation:{items:[]}} as never);
  const {rerender}=render(<Questions questions={[question]} mock={false} jobId="first-job"/>);
  await waitFor(()=>expect(screen.getByLabelText('第 1 题答案')).toHaveValue('old private answer'));
  rerender(<Questions questions={[question]} mock={false} jobId="second-job"/>);
  await waitFor(()=>expect(screen.getByLabelText('第 1 题答案')).toHaveValue(''));
});
