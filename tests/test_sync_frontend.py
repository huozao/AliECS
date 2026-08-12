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
              return elements[id]||(elements[id]={id,value:'',innerHTML:'',textContent:'',disabled:false,
                classList:{add(){},remove(){},toggle(){}},onclick:null,onchange:null});
            }
            const ids=['providerFilter','statusFilter','jobFilter','timelinePrevBtn','timelineNextBtn',
              'timelinePageInfo','timelineList','syncSummary','jobList','alertList','refreshBtn','loginBtn','logoutBtn'];
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
            const context={console,Promise,URLSearchParams,location:{search:'',pathname:'/sync/'},
              document:{getElementById:element},AliECSToast:{show(){}},
              AliECSAdmin:{api,fetchMe:async()=>null,esc,fmtTime:(value)=>value||'-',
                chip:(value)=>`<span>${value}</span>`,clearAuthToken(){},ssoLogin(){},applyGate(){}},
              setTimeout,clearTimeout};
            ids.forEach((id)=>{context[id]=element(id);});
            vm.createContext(context);
            vm.runInContext(source,context);
            function run(name){return {id:1,display_name:name,job_key:name,provider:'wecom',trigger:'manual',
              status:'success',started_at:'2026-08-12T00:00:00Z',duration_seconds:1,row_count:1,changed_count:0,
              error_label:'',error_message:''};}
            """
        ) + scenario
        result = subprocess.run(
            ["node", "-e", harness, str(SYNC_PAGE)],
            text=True,
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


if __name__ == "__main__":
    unittest.main()
