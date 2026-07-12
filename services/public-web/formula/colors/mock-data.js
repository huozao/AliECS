const RESINS = [
  {code:'ABS-757',name:'ABS 757',batch:'ABS-2407',baseline:[82.4,1.8,5.5]},
  {code:'PP-T30S',name:'PP T30S',batch:'PP-2411',baseline:[88.1,-0.8,3.2]},
  {code:'PA6-1013',name:'PA6 1013B',batch:'PA6-2409',baseline:[75.6,0.5,8.0]},
];

const FORMULAS = [
  {code:'HYD-RD-01',version:'V3',name:'胭脂红色粉',effect:[-22,42,18]},
  {code:'HYD-YL-02',version:'V2',name:'琥珀黄色粉',effect:[7,13,58]},
  {code:'HYD-BL-03',version:'V5',name:'靛青蓝色粉',effect:[-31,10,-48]},
  {code:'HYD-GR-04',version:'V1',name:'松石绿色粉',effect:[-12,-37,9]},
  {code:'HYD-VT-05',version:'V2',name:'暮紫色粉',effect:[-26,35,-24]},
];

const DOSAGES = [0.1,0.25,0.5,0.75,1.0];
const RESIN_RESPONSE = {
  'ABS-757':[1,1,1],
  'PP-T30S':[1.08,0.86,0.92],
  'PA6-1013':[0.9,1.14,1.08],
};

const round2 = (value)=>Math.round(value*100)/100;
const measurements=[];
let id=1;

for(const formula of FORMULAS){
  for(const resin of RESINS){
    for(const dosage of DOSAGES){
      const response=RESIN_RESPONSE[resin.code];
      const strength=Math.pow(dosage,0.72);
      const drift=((formula.code.charCodeAt(4)+resin.code.charCodeAt(0))*dosage%7-3)*0.12;
      const delta=formula.effect.map((value,index)=>round2(value*response[index]*strength+(index-1)*drift));
      const lab=resin.baseline.map((value,index)=>round2(value+delta[index]));
      measurements.push({
        id:id++,
        formula_key:`${formula.code}::${formula.version}`,
        parent_code:formula.code,
        parent_name:formula.name,
        formula_version:formula.version,
        resin_code:resin.code,
        resin_name:resin.name,
        resin_batch:resin.batch,
        dosage,
        dosage_unit:'%',
        condition_code:'D65-10-SCI',
        condition_name:'D65 · 10° · SCI · d/8',
        sample_lab:lab,
        baseline_lab:[...resin.baseline],
        measured_at:`2026-0${4+(id%3)}-${String(8+(id%19)).padStart(2,'0')}`,
        quality_status:'valid',
        source:'模拟数据',
      });
    }
  }
}

export const MOCK_RECIPE_COLORS={
  meta:{mode:'mock',generated_at:'2026-07-11',condition_code:'D65-10-SCI'},
  resins:RESINS,
  formulas:FORMULAS.map(({effect,...formula})=>({...formula,key:`${formula.code}::${formula.version}`})),
  measurements,
};
