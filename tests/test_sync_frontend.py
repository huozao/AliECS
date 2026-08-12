from __future__ import annotations

import subprocess
import textwrap
import unittest
from pathlib import Path


SYNC_PAGE = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "public-web"
    / "sync"
    / "index.html"
)


class SyncFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = SYNC_PAGE.read_text(encoding="utf-8") if SYNC_PAGE.exists() else ""

    def test_has_summary_jobs_timeline_drawer_and_alert_layers(self) -> None:
        for dom_id in (
            "syncSummary",
            "jobList",
            "timelineList",
            "timelinePager",
            "runDrawer",
            "alertList",
        ):
            self.assertIn(f'id="{dom_id}"', self.html)

    def test_uses_shared_admin_assets_and_gate_contract(self) -> None:
        for marker in (
            'href="/common/admin.css"',
            'src="/common/toast.js"',
            'src="/common/admin-auth.js"',
            'id="loginBtn"',
            'id="logoutBtn"',
            'id="refreshBtn"',
            'id="gateHint"',
            'id="adminContent"',
        ):
            self.assertIn(marker, self.html)
        self.assertIn("AliECSAdmin", self.html)
        self.assertIn("AliECSAdmin.applyGate", self.html)
        self.assertNotIn("async function api(", self.html)
        self.assertNotIn("function fmtTime(", self.html)
        self.assertNotIn("function chip(", self.html)
        self.assertNotIn("--bg:#f7f5f0", self.html)

    def test_uses_only_read_only_sync_endpoints(self) -> None:
        for path in ("/v1/sync/overview", "/v1/sync/runs", "/v1/sync/alerts"):
            self.assertIn(path, self.html)
        for write_marker in ("method:'POST'", 'method: "POST"', "method:'PUT'", 'method: "PUT"'):
            self.assertNotIn(write_marker, self.html)

    def test_has_global_filters_paging_and_query_preselection(self) -> None:
        for dom_id in (
            "providerFilter",
            "statusFilter",
            "jobFilter",
            "timelinePrevBtn",
            "timelineNextBtn",
            "timelinePageInfo",
        ):
            self.assertIn(f'id="{dom_id}"', self.html)
        self.assertIn("URLSearchParams(location.search)", self.html)
        self.assertIn(".get('job')", self.html)
        self.assertIn("params.set('provider'", self.html)
        self.assertIn("params.set('status'", self.html)
        self.assertIn("params.set('job_key'", self.html)
        self.assertIn("state.offset", self.html)

    def test_renders_unmonitored_and_explicit_empty_alert_state(self) -> None:
        self.assertIn("unmonitored", self.html)
        self.assertIn("未监控", self.html)
        self.assertIn("暂无未解决告警", self.html)

    def test_dynamic_api_text_is_escaped_without_raw_detail_dump(self) -> None:
        for field in (
            "display_name",
            "job_key",
            "provider",
            "error_label",
            "error_message",
            "alert_kind",
        ):
            self.assertIn(f"esc(item.{field}", self.html)
        self.assertNotIn("JSON.stringify", self.html)
        self.assertNotIn("detail_json", self.html)

    def test_future_write_actions_are_disabled_without_handlers(self) -> None:
        self.assertIn('disabled title="后续阶段开放"', self.html)
        self.assertNotIn("runJob", self.html)
        self.assertNotIn("saveJob", self.html)

    def test_detail_drawer_contract_is_present(self) -> None:
        for dom_id in ("runDrawerCloseBtn", "runDetailBody"):
            self.assertIn(f'id="{dom_id}"', self.html)
        self.assertIn("/v1/sync/runs/${runId}", self.html)
        self.assertIn("function openRunDetail(", self.html)
        self.assertIn("function renderSteps(", self.html)

    def _run_timeline_probe(self, scenario: str) -> None:
        harness = textwrap.dedent(
            r"""
            const fs=require('fs');
            const vm=require('vm');
            const html=fs.readFileSync(process.argv[1],'utf8');
            const start=html.indexOf('<script>')+8;
            const end=html.indexOf('</script>',start);
            if(start<8||end<0)throw new Error('inline script missing');
            const source=html.slice(start,end);
            const elements={};
            function element(id){
              const classes=new Set();
              return elements[id]||(elements[id]={id,value:'',innerHTML:'',textContent:'',disabled:false,
                classList:{add(...names){names.forEach((name)=>classes.add(name));},
                  remove(...names){names.forEach((name)=>classes.delete(name));},
                  toggle(name){classes.has(name)?classes.delete(name):classes.add(name);},
                  contains(name){return classes.has(name);}},onclick:null,onchange:null});
            }
            const ids=['providerFilter','statusFilter','jobFilter','timelinePrevBtn','timelineNextBtn',
              'timelinePageInfo','timelineList','syncSummary','jobList','alertList','refreshBtn','loginBtn','logoutBtn',
              'runDrawer','runDrawerCloseBtn','runDetailBody'];
            ids.forEach(element);
            const pending=[];
            function api(path){
              let resolve,reject;
              const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
              pending.push({path,resolve,reject,promise});
              return promise;
            }
            const esc=(value)=>String(value??'').replace(/[&<>"']/g,(char)=>({
              '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;',
            }[char]));
            const toasts=[];
            const timers=new Map();
            let nextTimerId=1;
            function fakeSetTimeout(callback,delay){
              const id=nextTimerId++;
              timers.set(id,{callback,delay});
              return id;
            }
            function fakeClearTimeout(id){timers.delete(id);}
            function fireNextTimer(){
              const entry=timers.entries().next();
              if(entry.done)throw new Error('no pending timer');
              const [id,timer]=entry.value;
              timers.delete(id);
              timer.callback();
              return timer;
            }
            const windowHandlers={};
            const context={console,Promise,URLSearchParams,location:{search:'',pathname:'/sync/'},
              document:{getElementById:element},AliECSToast:{show(text,type){toasts.push({text,type});}},
              AliECSAdmin:{api,fetchMe:async()=>null,esc,fmtTime:(value)=>value||'-',
                chip:(value)=>`<span>${value}</span>`,clearAuthToken(){},ssoLogin(){},applyGate(){}},
              window:{addEventListener(type,handler){windowHandlers[type]=handler;}},
              setTimeout:fakeSetTimeout,clearTimeout:fakeClearTimeout};
            ids.forEach((id)=>{context[id]=element(id);});
            vm.createContext(context);
            vm.runInContext(source,context);
            function run(name){return {id:1,display_name:name,job_key:name,provider:'wecom',trigger:'manual',
              status:'success',started_at:'2026-08-12T00:00:00Z',duration_seconds:1,row_count:1,changed_count:0,
              error_label:'',error_message:''};}
            function detail(id,status,options={}){
              const finished=status==='running'?null:'2026-08-12T00:00:03Z';
              return {run:{id,job_key:`job-${id}`,display_name:options.name||`run-${id}`,provider:'wecom',
                kind:'document',trigger:'manual',status,started_at:'2026-08-12T00:00:00Z',finished_at:finished,
                row_count:3,changed_count:1,error_kind:options.errorKind||null,error_label:options.errorLabel||'',
                error_message:options.errorMessage||'',detail_json:{secret:'must-not-render'},
                legacy_ref:{external_doc_id:'must-not-render'},duration_seconds:status==='running'?2:3},
                steps:[{seq:1,name:options.stepName||'fetch',status:options.stepStatus||status,
                  started_at:'2026-08-12T00:00:00Z',finished_at:finished,items:3,
                  message:options.stepMessage||null,duration_seconds:999}],
                reconciliation_id:null};
            }
            """
        ) + scenario
        result = subprocess.run(
            ["node", "-e", harness, str(SYNC_PAGE)],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)

    def test_older_timeline_response_cannot_overwrite_latest_page(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  const first=vm.runInContext('state.offset=20;loadTimeline()',context);
                  const second=vm.runInContext('state.offset=40;loadTimeline()',context);
                  if(pending.length!==2)throw new Error(`expected 2 requests, got ${pending.length}`);
                  pending[1].resolve({items:[run('newer-page')],total:100});
                  await second;
                  pending[0].resolve({items:[run('older-page')],total:100});
                  await first;
                  if(!elements.timelineList.innerHTML.includes('newer-page'))throw new Error('latest result missing');
                  if(elements.timelineList.innerHTML.includes('older-page'))throw new Error('older response overwrote latest');
                  if(!elements.timelinePageInfo.textContent.includes('第 3/5 页'))throw new Error(`wrong page: ${elements.timelinePageInfo.textContent}`);
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_pager_stays_disabled_and_does_not_double_advance_while_loading(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  vm.runInContext('state.total=100;renderTimeline()',context);
                  elements.timelineNextBtn.onclick();
                  elements.timelineNextBtn.onclick();
                  if(pending.length!==1)throw new Error(`expected 1 request, got ${pending.length}`);
                  if(!elements.timelinePrevBtn.disabled||!elements.timelineNextBtn.disabled)throw new Error('pager not loading-guarded');
                  if(!pending[0].path.includes('offset=20'))throw new Error(`wrong request: ${pending[0].path}`);
                  pending[0].resolve({items:[run('page-two')],total:100});
                  await pending[0].promise;
                  await new Promise((resolve)=>setTimeout(resolve,0));
                  if(vm.runInContext('state.offset',context)!==20)throw new Error('offset advanced twice');
                  if(elements.timelineNextBtn.disabled)throw new Error('pager did not recover after latest response');
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_failed_next_page_keeps_committed_rows_offset_and_pager(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  vm.runInContext("state.runs=[{id:1,display_name:'page-one',job_key:'page-one',provider:'wecom',trigger:'manual',status:'success',started_at:'2026-08-12T00:00:00Z',duration_seconds:1,row_count:1,changed_count:0,error_label:'',error_message:''}];state.total=100;state.offset=0;renderTimeline()",context);
                  elements.timelineNextBtn.onclick();
                  if(pending.length!==1)throw new Error(`expected 1 request, got ${pending.length}`);
                  if(!pending[0].path.includes('offset=20'))throw new Error(`wrong request: ${pending[0].path}`);
                  pending[0].reject(new Error('page failed'));
                  await pending[0].promise.catch(()=>{});
                  await new Promise((resolve)=>setTimeout(resolve,0));
                  if(!elements.timelineList.innerHTML.includes('page-one'))throw new Error('committed rows changed');
                  if(vm.runInContext('state.offset',context)!==0)throw new Error('failed request committed offset');
                  if(!elements.timelinePageInfo.textContent.includes('第 1/5 页'))throw new Error(`wrong page: ${elements.timelinePageInfo.textContent}`);
                  if(elements.timelinePageInfo.textContent.includes('加载中'))throw new Error('loading label did not clear');
                  if(vm.runInContext('state.timelineLoading',context)!==false)throw new Error('loading state did not recover');
                  if(!elements.timelinePrevBtn.disabled||elements.timelineNextBtn.disabled)throw new Error('pager state does not match page one');
                  if(toasts.length!==1)throw new Error(`expected 1 toast, got ${toasts.length}`);
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_detail_renders_safe_run_and_step_fields(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  const opened=vm.runInContext('openRunDetail(7)',context);
                  if(pending.length!==1||pending[0].path!=='/v1/sync/runs/7')throw new Error('wrong detail request');
                  pending[0].resolve(detail(7,'failed',{name:'<run-seven>',errorKind:'write',
                    errorLabel:'写入失败',errorMessage:'<run-error>',stepStatus:'failed',
                    stepName:'<write>',stepMessage:'<step-error>'}));
                  await opened;
                  const body=elements.runDetailBody.innerHTML;
                  if(!elements.runDrawer.classList.contains('show'))throw new Error('drawer did not open');
                  for(const expected of ['&lt;run-seven&gt;','写入失败','&lt;run-error&gt;',
                    '&lt;write&gt;','&lt;step-error&gt;','3 秒']){
                    if(!body.includes(expected))throw new Error(`missing safe field ${expected}: ${body}`);
                  }
                  for(const leaked of ['<run-seven>','<run-error>','<write>','<step-error>',
                    'must-not-render','external_doc_id','16 分 39 秒']){
                    if(body.includes(leaked))throw new Error(`unsafe/raw detail leaked: ${leaked}`);
                  }
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_closing_drawer_invalidates_late_detail_response(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  const opened=vm.runInContext('openRunDetail(1)',context);
                  elements.runDrawerCloseBtn.onclick();
                  pending[0].resolve(detail(1,'success',{name:'late-run'}));
                  await opened;
                  if(elements.runDrawer.classList.contains('show'))throw new Error('late response reopened drawer');
                  if(elements.runDetailBody.innerHTML.includes('late-run'))throw new Error('late response rendered after close');
                  if(vm.runInContext('openRunId',context)!==null)throw new Error('closed run remains active');
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_switching_runs_commits_only_latest_detail_response(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  const first=vm.runInContext('openRunDetail(1)',context);
                  const second=vm.runInContext('openRunDetail(2)',context);
                  pending[1].resolve(detail(2,'success',{name:'new-run'}));
                  await second;
                  pending[0].resolve(detail(1,'success',{name:'old-run'}));
                  await first;
                  const body=elements.runDetailBody.innerHTML;
                  if(!body.includes('new-run'))throw new Error('latest detail missing');
                  if(body.includes('old-run'))throw new Error('stale detail overwrote latest');
                  if(vm.runInContext('openRunId',context)!=='2')throw new Error('wrong active run');
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_stale_detail_error_cannot_replace_or_report_over_latest_run(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  const first=vm.runInContext('openRunDetail(1)',context);
                  const second=vm.runInContext('openRunDetail(2)',context);
                  pending[1].resolve(detail(2,'success',{name:'latest-run'}));
                  await second;
                  pending[0].reject(new Error('stale failure'));
                  await first;
                  const body=elements.runDetailBody.innerHTML;
                  if(!body.includes('latest-run')||body.includes('stale failure'))throw new Error('stale error changed detail');
                  if(toasts.length!==0)throw new Error('stale error emitted a toast');
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_running_poll_is_three_seconds_non_overlapping_and_stops_at_terminal(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  const opened=vm.runInContext('openRunDetail(3)',context);
                  pending[0].resolve(detail(3,'running'));
                  await opened;
                  if(timers.size!==1)throw new Error(`expected one poll timer, got ${timers.size}`);
                  if([...timers.values()][0].delay!==3000)throw new Error('poll delay is not 3000ms');
                  fireNextTimer();
                  await Promise.resolve();
                  if(pending.length!==2||pending[1].path!=='/v1/sync/runs/3')throw new Error('poll request missing');
                  if(timers.size!==0)throw new Error('poll overlapped in-flight request');
                  pending[1].resolve(detail(3,'success'));
                  await Promise.resolve();await Promise.resolve();
                  if(timers.size!==0)throw new Error('terminal run kept polling');
                  if(pending.length!==4)throw new Error(`terminal refresh missing: ${pending.length}`);
                  if(pending[2].path!=='/v1/sync/overview'||!pending[3].path.startsWith('/v1/sync/runs?')){
                    throw new Error(`wrong terminal refresh: ${pending.slice(2).map((item)=>item.path)}`);
                  }
                  pending[2].resolve({summary:{},items:[]});
                  pending[3].resolve({items:[],total:0});
                  await Promise.resolve();await Promise.resolve();
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_poll_error_retries_after_delay_and_close_or_unload_cancels_timer(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  const opened=vm.runInContext('openRunDetail(4)',context);
                  pending[0].resolve(detail(4,'running'));
                  await opened;
                  fireNextTimer();
                  await Promise.resolve();
                  pending[1].reject(new Error('network down'));
                  await pending[1].promise.catch(()=>{});
                  await Promise.resolve();await Promise.resolve();
                  if(toasts.length!==1||!toasts[0].text.includes('network down'))throw new Error('poll error not surfaced');
                  if(timers.size!==1||[...timers.values()][0].delay!==3000)throw new Error('poll error did not retry after delay');
                  windowHandlers.beforeunload();
                  if(timers.size!==0)throw new Error('beforeunload did not cancel poll');

                  const reopened=vm.runInContext('openRunDetail(5)',context);
                  pending[2].resolve(detail(5,'running'));
                  await reopened;
                  if(timers.size!==1)throw new Error('reopened running detail did not poll');
                  elements.runDrawerCloseBtn.onclick();
                  if(timers.size!==0)throw new Error('close did not cancel poll');
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )


if __name__ == "__main__":
    unittest.main()
