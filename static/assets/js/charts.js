/*!
 * charts.js – Pastell-Themes (Light/Dark), Hi-DPI, reaktiv auf Theme-Wechsel
 * Charts: Donut (Kategorien), Top-10 (Ausgaben), Fix vs. Variabel
 * Erwartet: Chart.js UMD + window.App (init(): window.App = this)
 */
(function(){
  // --------- Breakpoint Helper für mobile Darstellung ---------
  function isMobile(){
    return window.matchMedia("(max-width: 700px)").matches;
  }

  // Palette live aus CSS-Variablen lesen → reagiert auf Theme-Toggle
  function cssVar(name){
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function paletteFromCSS(){
    return {
      text:   cssVar('--chart-text')   || '#1F2430',
      grid:   cssVar('--chart-grid')   || 'rgba(31,36,48,.08)',
      border: cssVar('--chart-border') || '#E3D9C9',
      acc:   [cssVar('--acc1'), cssVar('--acc2'), cssVar('--acc3'), cssVar('--acc4'), cssVar('--acc5')].map(c=>c||'#999')
    };
  }

  // Chart Defaults
  if (typeof Chart !== "undefined") {
    Chart.defaults.font.family = '"Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
    Chart.defaults.responsive = true;
    Chart.defaults.maintainAspectRatio = false;         // Höhe via CSS
    Chart.defaults.animation.duration = 250;
    Chart.defaults.devicePixelRatio = () =>
      Math.max(1.5, Math.min(window.devicePixelRatio || 1, 3));
  }

  // Helpers
  const num      = v => Number(v || 0);
  const eur      = v => new Intl.NumberFormat('de-DE',{style:'currency',currency:'EUR'}).format(Number(v||0));
  const shorten  = (s,n=28)=> (s && s.length>n) ? s.slice(0,n-1)+'…' : s;
  const isExpense= l => l && l.type === 'expense';

  function totalOfLine(l){
    const subs = Array.isArray(l?.subitems) ? l.subitems : [];
    if (subs.length) return subs.reduce((s,x)=> s + num(x.amount), 0);
    return num(l?.base_amount ?? l?.amount ?? l?.value);
  }

  function linesFromApp(){
    if (window.App && Array.isArray(App.filteredGroups))
      return App.filteredGroups.flatMap(g=>g.lines) || [];
    if (window.App && Array.isArray(App.lines))
      return App.lines;
    if (Array.isArray(window.lines))
      return window.lines;
    return [];
  }

  function groupExpensesByCategory(lines){
    const m=new Map();
    (lines||[]).filter(isExpense).forEach(l=>{
      const k=String(l.category||'ohne Kategorie');
      m.set(k, (m.get(k)||0) + totalOfLine(l));
    });
    return [...m.entries()].sort((a,b)=>b[1]-a[1]);
  }

  function top10(lines){
    return (lines||[])
      .filter(isExpense)
      .map(l=>({label:l.label||l.title||'(ohne Bezeichnung)', value: totalOfLine(l)}))
      .sort((a,b)=>b.value-a.value)
      .slice(0,10);
  }

  function fixedVsVariable(lines){
    let fixed=0, variable=0;
    for(const l of (lines||[]).filter(isExpense)){
      const total   = totalOfLine(l);
      const hasSubs = Array.isArray(l.subitems) && l.subitems.length>0;
      if (l.is_variable===true)       variable+=total;
      else if (l.is_variable===false) fixed+=total;
      else if (hasSubs)               variable+=total;
      else                            fixed+=total;
    }
    return {fixed,variable};
  }

  const destroy = ch => { if(ch) try{ ch.destroy() }catch{} };

  // Instances
  let CH_DONUT=null, CH_TOP=null, CH_FIX=null;

  // --------- Donut: Ausgaben nach Kategorie ---------
  function renderDonut(lines, onClick){
    const P = paletteFromCSS();
    const data = groupExpensesByCategory(lines);
    const labels = data.map(([k])=>k);
    const values = data.map(([_,v])=>v);
    const el = document.getElementById('donutByCategory');
    if(!el || typeof Chart==='undefined') return;

    destroy(CH_DONUT);
    CH_DONUT = new Chart(el, {
      type:'doughnut',
      data:{
        labels,
        datasets:[{
          data: values,
          backgroundColor: labels.map((_,i)=> P.acc[i%P.acc.length]),
          borderColor: P.border,
          borderWidth: 2,
          hoverOffset: 8
        }]
      },
      options:{
        plugins:{
          legend:{
            position:'right',
            labels:{
              boxWidth:14,
              boxHeight:14,
              color:P.text,
              font:{size:12,weight:'600'}
            }
          },
          tooltip:{
            callbacks:{
              label: ctx => `${ctx.label}: ${eur(ctx.parsed)}`
            }
          }
        },
        cutout:'58%',
      }
    });

    // Click -> Kategorie filtern
    el.onclick = (e)=>{
      const pts = CH_DONUT.getElementsAtEventForMode(e,'nearest',{intersect:true},true);
      if(!pts?.length) return;
      onClick(labels[pts[0].index]);
    };
  }

  // --------- Top-10 Ausgabeposten (horizontal bar) ---------
  function renderTop(lines){
    const P = paletteFromCSS();
    const rows   = top10(lines);

    // Daten
    const labels = rows.map(r=>shorten(r.label, 40));
    const values = rows.map(r=>r.value);

    // Responsive Balken-Style
    const mobile = isMobile();
    const barThickness        = mobile ? 14 : 22;   // dünner auf Handy
    const maxBarThickness     = mobile ? 18 : 28;
    const categoryPercentage  = mobile ? 0.6 : 0.8; // mehr Luft zwischen Kategorien
    const barPercentage       = mobile ? 0.6 : 0.8;

    const el = document.getElementById('top10Expenses');
    if(!el || typeof Chart==='undefined') return;

    destroy(CH_TOP);
    CH_TOP = new Chart(el,{
      type:'bar',
      data:{
        labels,
        datasets:[{
          data: values,
          backgroundColor:P.acc[0],
          borderRadius:10,
          // responsive thickness
          barThickness: barThickness,
          maxBarThickness: maxBarThickness,
          categoryPercentage: categoryPercentage,
          barPercentage: barPercentage
        }]
      },
      options:{
        indexAxis:'y',
        plugins:{ legend:{display:false} },
        scales:{
          x:{
            grid:{color:P.grid},
            ticks:{
              callback:v=>eur(v),
              color:P.text,
              font:{size: mobile?11:12, weight:'500'}
            }
          },
          y:{
            grid:{display:false},
            ticks:{
              color:P.text,
              font:{size: mobile?12:12, weight:'600'},
              // label ggf. kürzen zusätzlich auf ultraklein
              callback:(val, i)=>{
                const full = this.getLabelForValue
                  ? this.getLabelForValue(val)
                  : labels[i] || val;
                if(mobile && full && full.length>28){
                  return full.slice(0,27)+'…';
                }
                return full;
              }
            }
          }
        },
        layout:{
          padding:{top:8,right:8,bottom:8,left:8}
        }
      }
    });
  }

  // --------- Fix vs. Variabel (vertical bar) ---------
  function renderFix(lines){
    const P = paletteFromCSS();
    const {fixed,variable} = fixedVsVariable(lines);

    const mobile = isMobile();
    const barThickness        = mobile ? 32 : 44;   // schmaler auf Handy
    const maxBarThickness     = mobile ? 40 : 56;
    const categoryPercentage  = mobile ? 0.5 : 0.7; // mehr Abstand zwischen Säulen
    const barPercentage       = mobile ? 0.5 : 0.7;

    const el = document.getElementById('fixedVsVariable');
    if(!el || typeof Chart==='undefined') return;

    destroy(CH_FIX);
    CH_FIX = new Chart(el,{
      type:'bar',
      data:{
        labels:['Fix','Variabel'],
        datasets:[{
          data:[fixed,variable],
          backgroundColor:[P.acc[0], P.acc[1]],
          borderRadius:10,
          barThickness: barThickness,
          maxBarThickness: maxBarThickness,
          categoryPercentage: categoryPercentage,
          barPercentage: barPercentage
        }]
      },
      options:{
        plugins:{legend:{display:false}},
        scales:{
          x:{
            grid:{display:false},
            ticks:{
              color:P.text,
              font:{size: mobile?13:12, weight:'700'}
            }
          },
          y:{
            grid:{color:P.grid},
            ticks:{
              color:P.text,
              callback:v=>eur(v),
              font:{size: mobile?11:12, weight:'500'}
            }
          }
        },
        layout:{
          padding:{top:8,right:8,bottom:8,left:8}
        }
      }
    });
  }

  // --------- Redraw orchestration ---------
  function renderAll(){
    const lines = linesFromApp();
    renderDonut(lines, cat => applyCategoryFilter(cat));
    renderTop(lines);
    renderFix(lines);
  }

  // Donut → Filter
  window.applyCategoryFilter = function(cat){
    const next = (window._activeCategoryFilter===cat ? null : cat);
    window._activeCategoryFilter = next;
    if(window.App){
      try{
        if(App.filter) App.filter.category = next || '';
        if(typeof App.compute === 'function') App.compute();
      }catch{}
    }
    setTimeout(renderAll,0);
  };

  // Lifecycle / Theme-Wechsel
  window.refreshFinanceCharts = renderAll;

  function whenReady(){
    let tries=0;
    const t=setInterval(()=>{
      tries++;
      if(document.getElementById('donutByCategory') || tries>120){
        clearInterval(t);
        renderAll();
      }
    },100);
  }

  (document.readyState==='loading')
    ? document.addEventListener('DOMContentLoaded', whenReady)
    : whenReady();

  document.addEventListener('themechange', ()=>{
    try { renderAll(); } catch {}
  });

  // Optional: Neu zeichnen bei Resize → damit mobile/desktop Balkenbreiten sich live anpassen,
  // wenn man das Fenster zieht oder dreht (Portrait/Landscape)
  window.addEventListener('resize', ()=>{
    try { renderAll(); } catch {}
  });
})();
