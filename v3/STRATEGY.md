# Strategie de Tranzacționare – Execuție & Managementul Riscului

> **Notă de context:** Toate orele din acest document sunt exprimate în fusul orar al României (**EET / EEST**). Strategia este optimizată pentru indici (în special **DAX** pentru sesiunea de dimineață) și necesită testare suplimentară înainte de aplicarea pe alte active.

---

## 1. Filtre Pre-Tranzacționare (Condiții Obligatorii)

Înainte de a căuta orice setup, trebuie îndeplinite simultan următoarele două condiții:

1. **Calendar Economic (Știri):** Se tranzacționează **doar în zilele fără știri macroeconomice majore** (fără evenimente de tip *Red Folder* care pot genera volatilitate haotică).
2. **Fereastra de Timp (Killzones):** Tranzacționarea este permisă exclusiv în intervalele active ale algoritmilor instituționali.

### Programul Sesiunilor & Intervale Permise
Sesiunile Sydney (22:00–07:00) și Tokyo (02:00–11:00) sunt ignorate. Ne concentrăm pe **Londra (10:00–19:00)** și **New York (15:00–00:00)**.

* 🚫 **09:45 – 10:15 | PAUZĂ** (Deschiderea Londrei la 10:00 – algoritmii se calibrează, risc ridicat de mișcări false).
* ✅ **10:15 – 16:15 | ACTIV – Liber pentru tranzacționare**
  * *Interval de aur:* **10:15 – 12:00** (recomandat în special pentru începători și pentru setup-urile pe DAX).
* 🚫 **16:15 – 16:45 | PAUZĂ** (Deschiderea sesiunii instituționale NY la 16:30 – volatilitate artificială).
  * *Regulă pentru poziții deschise înainte de 16:15:* Dacă tranzacția este în profit bun, se poate lăsa deschisă. Dacă stagnează sau are profit mic, se închide înainte de 16:15 și se caută reintrare după 16:45.
* ✅ **16:45 – 19:00 | ACTIV – Liber pentru tranzacționare**
* 🚫 **19:00 – 20:00 | PAUZĂ** (*New York Lunch Hour* – algoritmii instituționali sunt inactivi, lipsă de lichiditate).
* ✅ **20:00 – 21:30 | ACTIV – Liber pentru tranzacționare**
  * *Interval de aur:* **20:30 – 21:30** (frecvență ridicată a setup-urilor algoritmice).
  * *Notă:* Sesiunea NY merge până la 23:30, dar nu se recomandă deschiderea de poziții noi după 21:30.

---

## 2. Identificarea Setup-ului (Condiții de Intrare)

Pentru a valida o oportunitate de tranzacționare, prețul trebuie să îndeplinească **cel puțin una** dintre condițiile de lichiditate (A), combinată cu confirmările de structură (B).

### A. Captarea Lichidității (Liquidity Sweep)
Prețul trebuie să atingă și să lichideze o zonă importantă de lichiditate (majoră sau minoră).
* **Setup Specific HOD / LOD (High of Day / Low of Day):**
  * **Marcare:** Se identifică și se marchează cel mai înalt punct (HOD) și cel mai jos punct (LOD) al zilei formate **până la ora 09:59**.
  * **Execuție:** Se urmărește lichidarea HOD sau LOD în intervalul **10:15 – 12:00**.
  * **Activ recomandat:** Funcționează cel mai bine pe **DAX**. Pentru alte piețe, necesită backtesting.

### B. Confirmarea Instituțională (In orice ordine)
După sau în timpul luării lichidității, trebuie să observăm pe grafic următoarele 3 elemente (ordinea apariției lor nu este strictă, dar toate trebuie să fie prezente):
1. **MSS (Market Structure Shift):** Schimbare clară a structurii pieței (ruperea ultimului swing high/low relevant în direcția opusă).
2. **Displacement:** Mișcare impulsivă, agresivă a prețului (lumânări mari, pline, cu corp dominant, care arată implicare instituțională).
3. **Gap Instituțional (FVG - Fair Value Gap):** Dezechilibru creat în urma mișcării de *Displacement*, care va servi drept zonă de intrare (retestare).

---

## 3. Execuție și Managementul Riscului

Odată ce toate condițiile din secțiunile 1 și 2 sunt bifate, se plasează ordinul respectând strict parametrii de mai jos:

* **Intrare (Entry):** La retestarea *Gap-ului Instituțional (FVG)* sau imediat după confirmarea *MSS + Displacement*.
* **Stop Loss (SL):** Se plasează deasupra ultimului *High* relevant (pentru Short) sau sub ultimul *Low* relevant (pentru Long) care a generat mișcarea.
  * *Buffer de siguranță:* Se adaugă **1–2 pips/puncte** (sau un mic spațiu vizibil pe grafic) dincolo de extremitate pentru a evita scoaterea din tranzacție de către spread.
* **Take Profit (TP):** Raport Risk-to-Reward fix de **1:2 RR** sau **1:3 RR** (în funcție de următoarea zonă majoră de lichiditate).

---

## 4. Psihologie și Disciplină (Reguli de Aur)

* **Răbdare:** Așteaptă ca prețul să vină în zonele tale și în intervalele orare permise. Nu forța tranzacții în afara orelor stabilite.
* **Respectarea Planului:** Nu muta Stop Loss-ul pe pierdere mai mare și nu închide tranzacția impulsiv înainte de a atinge TP-ul sau SL-ul prestabilit (cu excepția regulii de la ora 16:15).
* **Consistență:** Tratează fiecare setup ca pe o execuție statistică dintr-o serie de 100 de tranzacții.