# Synthora Terminology Guide - academic phrasing to plain English

> **Purpose**: shared reference for prompts and post-processing. Update it when a new high-frequency jargon pattern shows up.

---

## 1. Analytical phrasing

| Academic or internal phrasing | Prefer in user-facing English | Notes |
|------------|------------|------|
| assumption surfacing | key assumptions / what this depends on | |
| hidden-assumption mining | what is being assumed but not said | |
| counter-argument / negative case | the strongest case against it / what could go wrong | |
| second-order effects | knock-on effects / ripple effects | |
| cognitive divergence | different views / where people disagree | |
| divergence labeling | say where the disagreement is | |
| cost-benefit analysis framework | tradeoff analysis / is it worth it | |
| position spectrum | range of views / where each side stands | |
| premise disclosure | key assumptions | already plain enough |

---

## 2. Output titles

Use these English titles in final outputs:

| Internal title | Use in English output |
|---------|---------|
| executive summary | 📋 Key Takeaway |
| key findings | 🔍 Key Findings |
| detailed analysis | 📊 Detailed Analysis |
| disagreements and disputes | ⚖️ Disagreements |
| practical advice | 💡 Recommendations |
| source index | 🔗 Source Index |
| further research directions | 🧭 Further Research |
| fact-check summary | Fact-Check Summary |

---

## 3. Technical terms that need a plain-English gloss

| Term | Example gloss |
|-----|--------------|
| net present value | NPV (how much future money is worth today) |
| differential equation | differential equation (a math equation about how things change over time) |
| elliptical integral | elliptical integral (a class of integrals without a simple closed-form answer) |
| Torricelli's law | Torricelli's law (the speed of water leaving a hole depends on water depth) |

---

## 4. Keys and fields that MUST stay untranslated

These are consumed by the frontend or structured parsers. Keep them exactly as written:

- `xKey`
- `yKeys`
- `Name`
- `Value`

For chart JSON, use:

```json
{
  "data": [
    {"Name": "A", "Value": 85}
  ],
  "xKey": "Name",
  "yKeys": ["Value"]
}
```

---

## 5. Terms that are already plain enough

These do not need forced simplification in English:

- AI
- algorithm
- data
- model
- probability
- assumption

---

## 6. Technical-question note

For `TECHNICAL`, `CODING`, and `MATH` questions, domain terminology often protects precision, so replacement is **not** mandatory.
Even then, explain unfamiliar jargon the first time it appears if a non-expert reader may not know it.
