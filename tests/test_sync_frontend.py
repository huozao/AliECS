from __future__ import annotations

import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYNC_PAGE = (
    ROOT
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
            "assetList",
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

    def test_uses_canonical_read_and_control_endpoints(self) -> None:
        for path in (
            "/v1/sync/overview", "/v1/sync/runs", "/v1/sync/alerts", "/v1/sync/assets",
            "/v1/sync/config/doc", "/v1/sync/config/tplus", "/v1/sync/run-all",
        ):
            self.assertIn(path, self.html)
        self.assertIn("method:'POST'", self.html)
        self.assertIn("method:'PUT'", self.html)
        self.assertNotIn("/v1/ops/doc-sync", self.html)
        self.assertNotIn("/v1/exports/sync-all", self.html)
        self.assertIn("/copy`,{method:'POST'", self.html)
        self.assertIn("/docid`,{method:'PUT'", self.html)
        self.assertIn("AliECSAdmin.downloadExport", self.html)

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
        self.assertIn(".get('group')", self.html)

    def test_jobs_and_assets_have_grouped_filters(self) -> None:
        # 作业总览已并入同步资产：筛选器挂在资产上，不再有独立的作业筛选区。
        for dom_id in (
            "assetStatusFilter", "assetFreshnessFilter", "assetSearchFilter",
            "assetTabs", "assetList", "runAllBtn",
            "docConfigCard", "tplusConfigCard",
        ):
            self.assertIn(f'id="{dom_id}"', self.html)
        for marker in ("T+ ERP", "企微 A", "企微 B", "飞书", "不可自动同步"):
            self.assertIn(marker, self.html)

    def test_uses_real_p1_trigger_and_provider_literals(self) -> None:
        for marker in ("manual:'手动'", "schedule:'定时'", "event:'订阅变更'"):
            self.assertIn(marker, self.html)
        provider_block = self.html.split('id="providerFilter"', 1)[1].split("</select>", 1)[0]
        self.assertNotIn('<option value="tplus">', provider_block)

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
        self.assertNotIn("<pre>${esc(JSON.stringify", self.html)
        self.assertNotIn("detail_json", self.html)

    def test_write_actions_have_loading_guards(self) -> None:
        for marker in ("function runJob(", "function runAsset(", "function copyAsset(", "function repairAsset(", "function runAll(", "function saveConfig("):
            self.assertIn(marker, self.html)
        self.assertIn("controlBusy", self.html)
        self.assertIn("session!==sessionGeneration", self.html)
        self.assertIn("copyIdempotencyKeys", self.html)
        self.assertIn("crypto.randomUUID", self.html)

    def test_asset_actions_follow_capabilities_and_system_assets_are_download_only(self) -> None:
        for marker in ("item.can_download", "item.can_sync", "item.can_copy", "item.system_managed"):
            self.assertIn(marker, self.html)
        for label in ("下载", "创建副本", "补录 docid", "系统管理资产"):
            self.assertIn(label, self.html)
        self.assertNotIn("external_doc_id", self.html)
        self.assertNotIn("api_doc_id}</", self.html)

    def test_static_preview_does_not_publish_external_identifiers(self) -> None:
        preview = (ROOT / "preview" / "doc-sync-table-preview.html").read_text(encoding="utf-8")
        self.assertNotRegex(preview, r"docid:\s*dc[A-Za-z0-9_-]+")
        self.assertNotRegex(preview, r"sheet_id:\s*[A-Za-z0-9_-]+")

    def test_failed_copy_retry_reuses_one_idempotency_key(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  const first=vm.runInContext('copyAsset(17,null)',context).catch(()=>{});
                  await Promise.resolve();
                  if(pending.length!==1)throw new Error(`expected first copy request, got ${pending.length}`);
                  const firstBody=pending[0].path+':'+String(pending[0].promise);
                  const firstKey=vm.runInContext('copyIdempotencyKeys.get(17)',context);
                  pending[0].reject(new Error('network'));
                  await first;
                  const second=vm.runInContext('copyAsset(17,null)',context).catch(()=>{});
                  await Promise.resolve();
                  if(pending.length!==2)throw new Error(`expected retry request, got ${pending.length}`);
                  const secondKey=vm.runInContext('copyIdempotencyKeys.get(17)',context);
                  if(!firstKey||firstKey!==secondKey)throw new Error('copy retry changed idempotency key');
                  pending[1].reject(new Error('network-again'));
                  await second;
                  if(firstBody.split(':')[0]!==pending[1].path)throw new Error('copy retry changed endpoint');
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_copy_success_is_not_reported_as_failure_when_refresh_rejects(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  let rejected=false;
                  const action=vm.runInContext('copyAsset(17,null)',context).catch(()=>{rejected=true;});
                  await Promise.resolve();
                  pending[0].resolve({status:'registered'});
                  await Promise.resolve();await Promise.resolve();
                  if(pending.length!==7)throw new Error(`expected copy plus six refresh requests, got ${pending.length}`);
                  pending[1].reject(new Error('refresh unavailable'));
                  for(let i=2;i<7;i++)pending[i].resolve(i===2?{items:[]}:(i===6?{items:[],total:0}:{}));
                  await action;
                  if(rejected)throw new Error('copy promise rejected after successful provider response');
                  if(vm.runInContext('copyIdempotencyKeys.has(17)',context))throw new Error('successful copy key retained');
                  if(!toasts.some((item)=>item.text.includes('副本已创建并登记')))throw new Error('success toast missing');
                  if(!toasts.some((item)=>item.text.includes('列表刷新失败')))throw new Error('refresh warning missing');
                  if(toasts.some((item)=>item.text.includes('创建副本失败')))throw new Error('copy falsely reported failed');
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_jobs_roll_up_into_document_rows_by_doc_source_id(self) -> None:
        """作业按文档级 source id 聚合，不按文档名——文档改名不能让作业错配到别的行。"""
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                vm.runInContext(`state.overview={summary:{},items:[
                  {job_key:'wecom.doc.19',provider:'wecom',display_name:'A/表1',enabled:true,source_id:19,
                   doc_source_id:5,source_group:'wecom_company_a',document_name:'改名前',sheet_name:'表1',
                   last_run:{status:'success',started_at:'2026-08-19T08:00:00Z'},freshness:{state:'fresh'},open_alert_count:0},
                  {job_key:'wecom.doc.20',provider:'wecom',display_name:'A/表2',enabled:true,source_id:20,
                   doc_source_id:5,source_group:'wecom_company_a',document_name:'改名前',sheet_name:'表2',
                   last_run:{status:'success',started_at:'2026-08-19T09:00:00Z'},freshness:{state:'stale'},open_alert_count:0}
                ]};
                state.assets=[{key:'wecom_company_a',title:'企微A',items:[{
                  name:'改名后',source_id:5,sheets:2,jobs:2,updated_at:null,
                  can_download:false,can_sync:true,can_copy:false,system_managed:false,reason:''
                }]}];state.activeAssetGroup='wecom_company_a';renderAssets()`,context);
                const rendered=elements.assetList.innerHTML;
                if(!rendered.includes('改名后'))throw new Error('document row missing');
                // 新鲜度取最差的一档，否则一张过期的表会被其余新鲜的表盖掉。
                if(!rendered.includes('已过期'))throw new Error('worst freshness not rolled up');
                // 全部成功时不展开表级明细。
                if(rendered.includes('需要处理的表'))throw new Error('healthy document expanded sheet detail');
                if(rendered.includes('wecom.doc.19'))throw new Error('healthy document leaked job rows');
                """
            )
        )

    def test_failed_sheet_auto_expands_under_its_document(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                vm.runInContext(`state.overview={summary:{},items:[
                  {job_key:'wecom.doc.1',provider:'wecom',display_name:'产量统计/正常表',enabled:true,source_id:1,
                   manual_triggerable:true,
                   doc_source_id:10,source_group:'wecom_company_a',document_name:'产量统计',sheet_name:'正常表',
                   last_run:{status:'success',started_at:'2026-08-19T08:00:00Z'},freshness:{state:'fresh'},open_alert_count:0},
                  {job_key:'wecom.doc.2',provider:'wecom',display_name:'产量统计/公开的生产记录表',enabled:true,source_id:2,
                   manual_triggerable:true,
                   doc_source_id:10,source_group:'wecom_company_a',document_name:'产量统计',sheet_name:'公开的生产记录表',
                   last_run:{status:'partial',started_at:'2026-08-13T16:01:04Z',error_label:'未知错误',error_message:'sync failure'},
                   freshness:{state:'stale'},open_alert_count:1}
                ]};
                state.assets=[{key:'wecom_company_a',title:'企微A',items:[{
                  name:'产量统计',source_id:10,sheets:2,jobs:2,updated_at:null,
                  can_download:false,can_sync:true,can_copy:false,system_managed:false,reason:''
                }]}];state.activeAssetGroup='wecom_company_a';renderAssets()`,context);
                const rendered=elements.assetList.innerHTML;
                if(!rendered.includes('需要处理的表'))throw new Error('problem sheets not expanded');
                if(!rendered.includes('公开的生产记录表'))throw new Error('failing sheet name missing');
                if(!rendered.includes('sync failure'))throw new Error('failure reason missing');
                // 只列出问题表，正常表不跟着展开。
                if(rendered.includes('正常表'))throw new Error('healthy sheet listed in problem detail');
                if(!rendered.includes('重试这张表'))throw new Error('sheet-level retry action missing');
                // 文档级状态取最坏的一档。
                if(!rendered.includes('部分成功'))throw new Error('document status not degraded by failing sheet');
                """
            )
        )

    def test_overview_collapses_zero_metrics_into_one_status_line(self) -> None:
        """概况只讲三件事：多少作业、有没有异常、新鲜度这列能不能信。

        旧版把 10 个计数平铺成等大卡片，生产上 8 个恒为 0，占了约 600px 却没有信息。
        """
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                vm.runInContext(`state.overview={summary:{jobs:92,fresh:1,warning:0,stale:0,never:0,
                  unmonitored:91,failed:1,partial:0,running:0,skipped:60,open_alerts:1},items:[
                  {job_key:'wecom.doc.2',display_name:'A/表',source_group:'wecom_company_a',last_run:{status:'failed'}},
                  {job_key:'feishu.doc.1',display_name:'F/表',source_group:'feishu',last_run:{status:'success'}}
                ]};renderOverview()`,context);
                const summaryHtml=elements.syncSummary.innerHTML;
                if(!summaryHtml.includes('92'))throw new Error('job total missing');
                if(!summaryHtml.includes('失败 1'))throw new Error('failure chip missing');
                if(!summaryHtml.includes('未解决告警 1'))throw new Error('open alert chip missing');
                // 恒为 0 的档不占版面。
                if(summaryHtml.includes('部分成功'))throw new Error('zero-valued status rendered');
                if(summaryHtml.includes('运行中'))throw new Error('zero-valued status rendered');
                if(summaryHtml.includes('已过期')||summaryHtml.includes('从未成功'))throw new Error('zero-valued freshness rendered');
                // 91/92 没配 SLA 时必须写清楚，否则「新鲜 1」会被读成健康度。
                if(!summaryHtml.includes('未监控 91/92'))throw new Error('sla coverage not stated');
                if(!summaryHtml.includes('企微 A 1'))throw new Error('group composition missing');
                """
            )
        )

    def test_overview_says_no_anomaly_instead_of_rendering_zeros(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                vm.runInContext(`state.overview={summary:{jobs:3,fresh:3,warning:0,stale:0,never:0,
                  unmonitored:0,failed:0,partial:0,running:0,skipped:0,open_alerts:0},items:[
                  {job_key:'feishu.doc.1',display_name:'F/表',source_group:'feishu',last_run:{status:'success'}}
                ]};renderOverview()`,context);
                const summaryHtml=elements.syncSummary.innerHTML;
                if(!summaryHtml.includes('无异常'))throw new Error('healthy state not summarised');
                if(!summaryHtml.includes('3 个作业全部已配 SLA'))throw new Error('full sla coverage not stated');
                """
            )
        )

    def test_skipped_run_is_labelled_and_filterable(self) -> None:
        """整簿跳过必须能和「没跑」分开——这正是用户把企微 A/B 读成「都没同步」的原因。"""
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                vm.runInContext(`state.overview={summary:{},items:[
                  {job_key:'wecom.doc.7',provider:'wecom',display_name:'点餐表/表1',enabled:true,source_id:7,
                   manual_triggerable:true,doc_source_id:9,source_group:'wecom_company_a',
                   document_name:'点餐表',sheet_name:'表1',
                   last_run:{status:'skipped',started_at:'2026-08-28T00:30:00Z'},
                   freshness:{state:'unmonitored'},open_alert_count:0}
                ]};
                state.assets=[{key:'wecom_company_a',title:'企微A',items:[{
                  name:'点餐表',source_id:9,sheets:1,jobs:1,updated_at:'2026-08-14T00:00:00Z',
                  can_download:false,can_sync:true,can_copy:false,system_managed:false,reason:''
                }]}];state.activeAssetGroup='wecom_company_a';renderAssets()`,context);
                if(!elements.assetList.innerHTML.includes('已跳过·内容未变'))
                  throw new Error('skipped run not labelled');
                vm.runInContext("assetStatusFilter.value='skipped';renderAssets()",context);
                if(!elements.assetList.innerHTML.includes('点餐表'))throw new Error('skipped filter dropped the row');
                vm.runInContext("assetStatusFilter.value='failed';renderAssets()",context);
                if(elements.assetList.innerHTML.includes('点餐表'))throw new Error('skipped row leaked into failed filter');
                """
            )
        )

    def test_unreadable_records_are_flagged_without_failing_the_row(self) -> None:
        """企微对个别记录恒返回 60111，是数据源的稳定缺陷，常驻标注、不算同步失败。"""
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                vm.runInContext(`state.overview={summary:{},items:[
                  {job_key:'wecom.doc.2',provider:'wecom',display_name:'产量统计/公开的生产记录表',enabled:true,
                   source_id:2,manual_triggerable:true,doc_source_id:10,source_group:'wecom_company_a',
                   document_name:'产量统计',sheet_name:'公开的生产记录表',
                   last_run:{status:'success',started_at:'2026-08-28T00:30:00Z',unreadable_record_count:3},
                   freshness:{state:'unmonitored'},open_alert_count:0}
                ]};
                state.assets=[{key:'wecom_company_a',title:'企微A',items:[{
                  name:'产量统计',source_id:10,sheets:2,jobs:1,updated_at:'2026-08-28T00:30:00Z',
                  can_download:false,can_sync:true,can_copy:false,system_managed:false,reason:''
                }]}];state.activeAssetGroup='wecom_company_a';renderAssets()`,context);
                const rendered=elements.assetList.innerHTML;
                if(!rendered.includes('3 条不可读'))throw new Error('unreadable record count not surfaced');
                if(!rendered.includes('成功'))throw new Error('row should stay successful');
                if(rendered.includes('需要处理的表'))throw new Error('unreadable records must not open the problem panel');
                """
            )
        )

    def test_jobs_without_document_land_in_system_group(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                vm.runInContext(`state.overview={summary:{},items:[
                  {job_key:'wecom.locator_mirror',provider:'wecom',display_name:'定位档案镜像',enabled:true,
                   source_id:null,manual_triggerable:false,
                   doc_source_id:null,source_group:'wecom_',document_name:null,sheet_name:null,
                   last_run:null,freshness:{state:'unmonitored'},open_alert_count:0}
                ]};
                state.assets=[{key:'wecom_company_a',title:'企微A',items:[]}];
                state.activeAssetGroup='system';renderAssets()`,context);
                if(!elements.assetTabs.innerHTML.includes('系统任务'))throw new Error('system group tab missing');
                if(!elements.assetList.innerHTML.includes('定位档案镜像'))throw new Error('orphan job dropped from every group');
                // enabled=true 但 kind=mirror：后端 enqueue_doc_job 收不了它，按钮就不能出现，
                // 否则点下去必报「同步作业不存在或不可手动触发」。
                if(elements.assetList.innerHTML.includes('立即同步'))throw new Error('non-triggerable job rendered a sync button');
                if(!elements.assetList.innerHTML.includes('系统调度，不支持手动触发'))throw new Error('non-triggerable reason missing');
                """
            )
        )

    def test_system_asset_renders_download_without_sync_or_copy(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                vm.runInContext(`state.assets=[{key:'wecom_company_a',title:'企微A',items:[{
                  name:'backup',source_id:12,sheets:2,jobs:0,updated_at:null,
                  can_download:true,can_sync:false,can_copy:false,system_managed:true,
                  reason:'系统管理资产，仅提供下载',download_url:'/v1/exports/external-doc/12'
                }]}];state.activeAssetGroup='wecom_company_a';renderAssets()`,context);
                const renderedAssets=elements.assetList.innerHTML;
                if(!renderedAssets.includes('下载')||!renderedAssets.includes('系统管理资产'))throw new Error('system download action missing');
                if(renderedAssets.includes('立即同步')||renderedAssets.includes('创建副本'))throw new Error('system asset exposed write action');
                """
            )
        )

    def test_dynamic_job_keys_never_enter_inline_handlers(self) -> None:
        self.assertIn("function runProblemJob(", self.html)
        self.assertIn("function runVisibleAsset(", self.html)
        self.assertNotIn("onclick=\"runJob('${esc(item.job_key)}'", self.html)

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
            const ids=['providerFilter','statusFilter','jobFilter','assetStatusFilter','assetFreshnessFilter','assetSearchFilter','timelinePrevBtn','timelineNextBtn',
              'timelinePageInfo','timelineList','syncSummary','alertList','refreshBtn','loginBtn','logoutBtn',
              'runDrawer','runDrawerCloseBtn','runDetailBody','assetTabs','assetList','runAllBtn','docConfigEnabled','docConfigHours','docConfigAnchor','docConfigPaused','docConfigSaveBtn','docConfigStatus','tplusConfigEnabled','tplusConfigHours','tplusConfigAnchor','tplusConfigSaveBtn','tplusConfigStatus'];
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
                reconciliation_id:options.reconciliationId??null};
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

    def test_refresh_commits_only_latest_overview_alerts_and_timeline_batch(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  elements.refreshBtn.onclick();
                  elements.refreshBtn.onclick();
                  await Promise.resolve();
                  if(pending.length!==12)throw new Error(`loadAll must start 6 parallel reads per refresh, got ${pending.length}`);
                  const overview=(jobs)=>({summary:{jobs},items:[]});
                  const alerts=(name)=>({items:[{display_name:name,job_key:name,provider:'wecom',alert_kind:'failed',first_seen_at:'x',notify_count:1}]});
                  pending[6].resolve(overview(2));pending[7].resolve(alerts('new-alert'));pending[8].resolve({groups:[]});pending[9].resolve({});pending[10].resolve({});pending[11].resolve({items:[run('new-run')],total:1});
                  await Promise.resolve();await Promise.resolve();
                  pending[0].resolve(overview(1));pending[1].resolve(alerts('old-alert'));pending[2].resolve({groups:[]});pending[3].resolve({});pending[4].resolve({});pending[5].resolve({items:[run('old-run')],total:1});
                  await Promise.resolve();await Promise.resolve();
                  if(vm.runInContext('state.overview.summary.jobs',context)!==2)throw new Error('stale overview committed');
                  if(!elements.alertList.innerHTML.includes('new-alert')||elements.alertList.innerHTML.includes('old-alert'))throw new Error('stale alerts committed');
                  if(!elements.timelineList.innerHTML.includes('new-run')||elements.timelineList.innerHTML.includes('old-run'))throw new Error('stale timeline committed');
                  if(toasts.length!==0)throw new Error(`stale batch emitted ${toasts.length} toast(s)`);
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_stale_refresh_errors_do_not_toast_over_latest_batch(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  elements.refreshBtn.onclick();elements.refreshBtn.onclick();
                  await Promise.resolve();
                  pending[6].resolve({summary:{jobs:2},items:[]});pending[7].resolve({items:[]});pending[8].resolve({groups:[]});pending[9].resolve({});pending[10].resolve({});pending[11].resolve({items:[],total:0});
                  await Promise.resolve();await Promise.resolve();
                  for(let i=0;i<6;i++)pending[i].reject(new Error('old refresh failed'));
                  await Promise.resolve();await Promise.resolve();
                  if(vm.runInContext('state.overview.summary.jobs',context)!==2)throw new Error('latest batch was disturbed');
                  if(toasts.length!==0)throw new Error(`stale errors emitted ${toasts.length} toast(s)`);
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_real_schedule_and_event_triggers_render_chinese_labels(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (()=>{
                  const scheduled=run('scheduled-run');scheduled.trigger='schedule';
                  const event=run('event-run');event.trigger='event';
                  context.fixtureRuns=[scheduled,event];
                  vm.runInContext('state.runs=fixtureRuns;renderTimeline()',context);
                  const body=elements.timelineList.innerHTML;
                  if(!body.includes('定时')||!body.includes('订阅变更'))throw new Error(`real trigger labels missing: ${body}`);
                  if(body.includes('>schedule<')||body.includes('>event<'))throw new Error(`raw P1 trigger leaked: ${body}`);
                })();
                """
            )
        )

    def test_logout_invalidates_all_late_successes_and_errors_and_clears_admin_state(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  vm.runInContext("state.overview={summary:{jobs:7},items:[]};state.alerts=[{display_name:'secret',job_key:'secret',provider:'wecom',alert_kind:'failed'}];state.runs=[{id:1,display_name:'secret-run',job_key:'secret',provider:'wecom',trigger:'manual',status:'success'}];state.total=1;renderOverview();renderAlerts();renderTimeline()",context);
                  const overview=vm.runInContext('loadOverview()',context).catch(()=>{});
                  const alerts=vm.runInContext('loadAlerts()',context).catch(()=>{});
                  const timeline=vm.runInContext('loadTimeline()',context).catch(()=>{});
                  const detailRequest=vm.runInContext('openRunDetail(8)',context).catch(()=>{});
                  if(pending.length!==4)throw new Error(`expected four in-flight reads, got ${pending.length}`);
                  elements.logoutBtn.onclick();
                  pending[0].resolve({summary:{jobs:99},items:[]});
                  pending[1].reject(new Error('late alerts error'));
                  pending[2].resolve({items:[run('late-run')],total:1});
                  pending[3].reject(new Error('late detail error'));
                  await Promise.all([overview,alerts,timeline,detailRequest]);
                  const snapshot=vm.runInContext('({overview:state.overview,alerts:state.alerts,runs:state.runs,total:state.total,loading:state.timelineLoading,openRunId})',context);
                  if(snapshot.overview!==null||snapshot.alerts.length||snapshot.runs.length||snapshot.total!==0||snapshot.loading||snapshot.openRunId!==null)throw new Error(`logout retained admin state: ${JSON.stringify(snapshot)}`);
                  for(const id of ['syncSummary','assetList','alertList','timelineList','runDetailBody']){
                    if(elements[id].innerHTML)throw new Error(`logout retained ${id} DOM`);
                  }
                  if(elements.runDrawer.classList.contains('show')||timers.size)throw new Error('logout retained detail UI/poll');
                  if(toasts.length!==1||toasts[0].text!=='已退出登录。')throw new Error(`late request emitted toast: ${JSON.stringify(toasts)}`);
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

    def test_detail_fetches_and_renders_structured_reconciliation_safely(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  const opened=vm.runInContext('openRunDetail(7)',context);
                  pending[0].resolve(detail(7,'success',{name:'T+ run',reconciliationId:44}));
                  await new Promise((resolve)=>setImmediate(resolve));
                  if(pending.length!==2||pending[1].path!=='/v1/ops/reconciliation/44')throw new Error(`read-only reconciliation GET missing: ${pending.map(x=>x.path)}`);
                  pending[1].resolve({id:44,status:'needs_review',severity:'warning',summary:'<summary>',diff_json:{
                    added:[{parent_code:'P<1>',parent_name:'新增',version:'1',child_code:'C1',child_name:'<img src=x onerror=1>',unit:'kg',quantity:2,disabled:false}],
                    removed:[{parent_code:'P2',parent_name:'删除',version:'2',child_code:'C2',child_name:'旧件',unit:'kg',quantity:1,disabled:true}],
                    changed:[{key:{parent_code:'P3',version:'3',child_code:'C3'},changed_fields:['quantity','<script>'],before:{quantity:1,child_name:'old'},after:{quantity:2,child_name:'new'}}]
                  }});
                  await opened;
                  const body=elements.runDetailBody.innerHTML;
                  for(const expected of ['变化明细','needs_review','warning','&lt;summary&gt;','新增子件','删除子件','字段变化','P&lt;1&gt;','&lt;img src=x onerror=1&gt;','&lt;script&gt;']){
                    if(!body.includes(expected))throw new Error(`missing reconciliation field ${expected}: ${body}`);
                  }
                  for(const leaked of ['<summary>','<img src=x onerror=1>','<script>','原始 JSON']){
                    if(body.includes(leaked))throw new Error(`unsafe/raw reconciliation leaked: ${leaked}`);
                  }
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_stale_reconciliation_cannot_overwrite_newer_run_detail(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  const first=vm.runInContext('openRunDetail(1)',context);
                  pending[0].resolve(detail(1,'success',{name:'old-run',reconciliationId:11}));
                  await new Promise((resolve)=>setImmediate(resolve));
                  if(pending[1].path!=='/v1/ops/reconciliation/11')throw new Error('old reconciliation read missing');
                  const second=vm.runInContext('openRunDetail(2)',context);
                  pending[2].resolve(detail(2,'success',{name:'new-run'}));
                  await second;
                  pending[1].resolve({status:'needs_review',severity:'critical',summary:'old-diff',diff_json:{added:[],removed:[],changed:[]}});
                  await first;
                  const body=elements.runDetailBody.innerHTML;
                  if(!body.includes('new-run')||body.includes('old-run')||body.includes('old-diff'))throw new Error('stale reconciliation overwrote current detail');
                  if(toasts.length!==0)throw new Error('stale reconciliation emitted toast');
                })().catch((error)=>{console.error(error.stack||error);process.exitCode=1;});
                """
            )
        )

    def test_reconciliation_read_failure_keeps_run_and_steps_visible(self) -> None:
        self._run_timeline_probe(
            textwrap.dedent(
                r"""
                (async()=>{
                  const opened=vm.runInContext('openRunDetail(9)',context);
                  pending[0].resolve(detail(9,'success',{name:'kept-run',stepName:'kept-step',reconciliationId:91}));
                  await new Promise((resolve)=>setImmediate(resolve));
                  pending[1].reject(new Error('<private database error>'));
                  await opened;
                  const body=elements.runDetailBody.innerHTML;
                  for(const expected of ['kept-run','kept-step','差异明细读取失败'])if(!body.includes(expected))throw new Error(`missing ${expected}: ${body}`);
                  if(body.includes('private database error'))throw new Error('raw reconciliation error leaked');
                  if(toasts.length!==0)throw new Error('optional reconciliation failure emitted toast');
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
                  await new Promise((resolve)=>setImmediate(resolve));
                  if(timers.size!==0)throw new Error('terminal run kept polling');
                  if(pending.length!==8)throw new Error(`terminal refresh missing: ${pending.length}`);
                  if(pending[2].path!=='/v1/sync/overview'||!pending[3].path.startsWith('/v1/sync/alerts?')||pending[4].path!=='/v1/sync/assets'||pending[5].path!=='/v1/sync/config/doc'||pending[6].path!=='/v1/sync/config/tplus'||!pending[7].path.startsWith('/v1/sync/runs?')){
                    throw new Error(`wrong terminal refresh: ${pending.slice(2).map((item)=>item.path)}`);
                  }
                  pending[2].resolve({summary:{},items:[]});
                  pending[3].resolve({items:[],total:0});pending[4].resolve({groups:[]});pending[5].resolve({});pending[6].resolve({});pending[7].resolve({items:[],total:0});
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
