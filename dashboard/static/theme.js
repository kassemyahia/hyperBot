"use strict";
(() => {
  const root = document.documentElement;
  function applyTheme(theme) {
    root.classList.toggle("light", theme === "light");
    document.querySelectorAll("[data-theme-toggle]").forEach(button => {
      button.textContent = theme === "light" ? "☾" : "☀";
      button.setAttribute("aria-label", theme === "light" ? "تفعيل الوضع الليلي" : "تفعيل الوضع النهاري");
    });
  }
  let theme = "dark";
  try { if (localStorage.getItem("hyperbot-theme") === "light") theme = "light"; } catch (_) {}
  root.classList.toggle("light", theme === "light");
  document.addEventListener("DOMContentLoaded", () => {
    applyTheme(theme);
    document.querySelectorAll("[data-theme-toggle]").forEach(button => button.addEventListener("click", () => {
      theme = theme === "dark" ? "light" : "dark";
      applyTheme(theme);
      try { localStorage.setItem("hyperbot-theme", theme); } catch (_) {}
    }));
    const search = document.getElementById("search");
    const catalog = document.querySelectorAll("#catalog-table tbody tr");
    if (search) search.addEventListener("input", () => {
      const query = search.value.trim().toLocaleLowerCase();
      catalog.forEach(row => { row.hidden = !row.textContent.toLocaleLowerCase().includes(query); });
    });
    const links = [...document.querySelectorAll("nav a[href^='#']")];
    function markNav() {
      const active = location.hash || "#files";
      links.forEach(link => {
        if (link.hash === active) link.setAttribute("aria-current", "location");
        else link.removeAttribute("aria-current");
      });
    }
    markNav(); window.addEventListener("hashchange", markNav);
    const calendar = document.getElementById("shift-calendar");
    const cards = [...document.querySelectorAll(".shift[data-date]")];
    if (!calendar || !cards.length) return;
    const byDate = new Map(cards.map(card => [card.dataset.date, card]));
    const dateKeys = [...byDate.keys()].filter(d => /^\d{4}-\d{2}-\d{2}$/.test(d)).sort();
    if (!dateKeys.length) return;
    const grid = document.getElementById("calendar-grid");
    const title = document.getElementById("calendar-month");
    const filter = document.getElementById("shift-search");
    const clear = document.getElementById("show-all-shifts");
    const prev = document.getElementById("calendar-prev"), next = document.getElementById("calendar-next");
    const empty = document.getElementById("shift-empty");
    const first = dateKeys[0].slice(0,7), last = dateKeys.at(-1).slice(0,7);
    let [year, month] = first.split("-").map(Number);
    let selected = "";
    function monthKey() { return year + "-" + String(month).padStart(2,"0"); }
    function update() {
      const query = filter.value.trim().toLocaleLowerCase();
      let visible = 0;
      cards.forEach(card => {
        card.hidden = (selected !== "" && selected !== card.dataset.date) || !card.textContent.toLocaleLowerCase().includes(query);
        if (!card.hidden) visible++;
      });
      empty.hidden = visible !== 0;
      grid.querySelectorAll("button[data-date]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.date === selected)));
    }
    function cell(text, className) {
      const span = document.createElement("span"); span.textContent = text; span.className = className || ""; grid.append(span);
    }
    function render() {
      title.textContent = new Intl.DateTimeFormat("ar-SY", {month:"long",year:"numeric",timeZone:"UTC"}).format(new Date(Date.UTC(year,month-1,1)));
      prev.disabled = monthKey() <= first; next.disabled = monthKey() >= last;
      grid.replaceChildren();
      ["أحد","اثن","ثلا","أرب","خمي","جمع","سبت"].forEach(day => cell(day,"weekday"));
      const offset = new Date(Date.UTC(year,month-1,1)).getUTCDay();
      for(let i=0;i<offset;i++) cell("");
      const count = new Date(Date.UTC(year,month,0)).getUTCDate();
      for(let day=1;day<=count;day++){
        const key = monthKey()+"-"+String(day).padStart(2,"0");
        if(!byDate.has(key)){cell(day);continue;}
        const button=document.createElement("button");
        button.type="button";button.className="has-shift";button.dataset.date=key;button.textContent=day;
        button.setAttribute("aria-label","مناوبة "+key);
        button.addEventListener("click",()=>{selected=selected===key?"":key;update();});
        grid.append(button);
      }
      update();
    }
    function move(step) {
      const date = new Date(Date.UTC(year,month-1+step,1));
      year=date.getUTCFullYear();month=date.getUTCMonth()+1;selected="";render();
    }
    prev.addEventListener("click",()=>move(-1));next.addEventListener("click",()=>move(1));
    clear.addEventListener("click",()=>{selected="";filter.value="";update();});
    filter.addEventListener("input",update);
    calendar.hidden=false;
    document.getElementById("shift-toolbar").hidden=false;
    render();
  });
})();
