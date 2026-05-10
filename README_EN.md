[🇰🇷 한국어](./README.md) | [🇺🇸 English](./README_EN.md)

# 🎯 Pickly — Ad Intelligence Platform

> **Prove your influencer marketing with data.**  
> An intelligence platform that collects and analyzes comments, views, and reactions from sponsored YouTube videos using AI.

---

## 📌 Overview

**Pickly** is a cloud-native B2B SaaS platform for brand marketers and marketing agencies running influencer (YouTube sponsored) campaigns.

It automatically detects real purchase intent and brand crisis signals hidden within tens of thousands of YouTube comments and shopping reviews, supporting data-driven decision making for marketers.

- **KcELECTRA**: A model specialized in Korean colloquial expressions and neologisms, used to classify tens of thousands of comments (positive / negative / crisis)
- **GPT-4o-mini**: Generates actionable insight summaries and handles RAG-based chatbot responses based on classified data

> A hybrid AI architecture — KcELECTRA handles high-volume, speed-sensitive sentiment classification, while GPT-4o-mini handles complex context understanding and insight generation.

---

## 🏗 System Architecture

**Data Flow**

```
youtube_collect.py / naver_collect.py / oliveyoung_collect.py
        ↓ (Azure Functions · Timer Trigger)
   PostgreSQL (Raw Data)
        ↓ (comment-analyzer · Azure Functions · KcELECTRA)
   PostgreSQL (Sentiment Analysis)
        ↓ (Stream Analytics · Crisis / Viral Detection)
   alert-func-v2 → Real-time Alert
        ↓
   app.py (Flask · GPT-4o-mini)
```

| Stage | Component | Details |
|-------|-----------|---------|
| Collect | `youtube_collect.py` | YouTube Data API comment collection · Timer Trigger every 6 hours |
| Process | `youtube_processing.py` | Data preprocessing · Load to PostgreSQL |
| Collect | `naver_collect.py` · `oliveyoung_collect.py` | Naver Shopping / Oliveyoung crawling · Every 12 hours · 50–150 records |
| Process | `platform_processing.py` | Platform review preprocessing · Load to PostgreSQL |
| AI Analysis | `comment-analyzer` (Azure Functions) | KcELECTRA sentiment classification · Purchase intent · Crisis flag |
| Real-time Detection | Stream Analytics (`comments-asa-job`) | Crisis comment surge · Viral spike detection |
| Alert | `alert-func-v2` (Azure Functions) | Webhook alert when crisis/viral condition is met |
| Visualization | `app.py` (Flask) | Web dashboard · GPT-4o-mini insight summary · Voice guidance |
| CI/CD | GitHub Actions | Auto build & deploy on `front` branch push |

### 🚨 Real-time Detection Logic (Stream Analytics)

**Crisis Detection (CRISIS_ALERT)**

`HoppingWindow(hour, 24, 6)` — Used to track continuous crisis trends by analyzing overlapping time ranges. Checks 24 hours of data every 6 hours, and sends an immediate webhook alert when 10 or more negative (`NEGATIVE`) + crisis-flagged (`crisis_flag = true`) comments accumulate. Catches rapidly deteriorating public sentiment within the golden response window.

**Viral Spike Detection (VIRAL_SPIKE)**

`TumblingWindow` — Used to measure sudden spikes by independently comparing data across fixed time intervals. Joins two windows to detect when the current 6-hour comment count exceeds the previous 6-hour count by **more than 3x (300%)**. Precisely identifies viral moments so marketers can time additional promotions.

---

## 🗄 Database Schema

Data quality is managed progressively across 3 layers.

### ① Collection Layer | Raw Data

![Collection Layer](./image/수집레이어.png)

### ② Analysis Layer | Cleansing & Sentiment

![Analysis Layer](./image/분석레이어.png)

### ③ Aggregation Layer | Metrics & Output

![Aggregation Layer](./image/집계레이어.png)

---

## ☁️ Azure Resources

### Resource Group (`3dt-1st-team1`)

![Resource Group](./image/리소스그룹.png)

### Azure OpenAI Model Deployment

![Azure OpenAI](./image/open_ai.png)

### PostgreSQL — fivegirls-db

![PostgreSQL](./image/db1.png)

- Config: Burstable B2ms · 2 vCores · 8 GiB RAM · 32 GiB Storage
- PostgreSQL version: 16.12 · Location: Canada Central

---

## 🚀 Azure Functions

![Azure Functions](./image/함수앱.png)

---

## 🚢 Deployment

### App Service Overview

![App Service](./image/웹앱_1.png)

- URL: `pickly-dashboard.azurewebsites.net`
- Runtime: Python 3.11 · Linux · App Service B1

### Deployment Center — GitHub Actions CI/CD

![Deployment Center](./image/웹앱_2.png)

- Org: `miyeon00` / Repo: `MS_FIVE_GIRLS` / Branch: `front`
- Auto build and deploy on every push to `front` branch

### Environment Variables

![Environment Variables](./image/웹앱_3.png)

---

## 📊 Dashboard Screenshots

### 1. Login / Sign Up

![Login](./image/화면8.png)

KO · EN multilingual support. Role-based access control (Brand Marketer / Agency / Admin).

---

### 2. Product List

![Product List](./image/화면1.png)

Overview of total products, videos, views, and analyzed comments. Click a product to navigate to the campaign dashboard.

---

### 3. Performance Hub — Real-time KPI

![Performance Hub](./image/화면2.png)

- Crisis comment banner + real-time data collection status
- KPI: Total views **730K** · Total comments **2,850** · Positive rate **89.5%** · Purchase intent **1,272** · Crisis comments **158**
- **AI INSIGHTS** ticker: GPT-4o-mini powered actionable insights auto-generated
- Right panel: RAG-based AI data assistant chatbot

---

### 4. KPI Summary — Per Video Comparison

![KPI Summary](./image/화면3.png)

Compare positive rate, purchase intent, crisis comments, and collaboration score across videos. Best/risk badges auto-displayed.

---

### 5. Platform Reviews & Sentiment Trend

![Platform Reviews](./image/화면4.png)

- Pre/post-campaign review score comparison (Naver Shopping vs. Oliveyoung)
- Positive/negative trend area chart by time period (within 24h / 1–3 days / 3–7 days / 7+ days)
- 💡 Average rating increased **+0.03** after campaign launch; reactions concentrated in the 7-day+ window (4,792 reviews)

---

### 6. VOC Analysis — Strength / Weakness Keywords

![VOC Analysis](./image/화면5.png)

| Category | Top Keywords |
|----------|-------------|
| Positive VOC | Darkening(586) · Shade 21(460) · Pores(447) · Dry(378) · Matte(351) |
| Negative VOC | Dry(14) · Oily(14) · Shade 21(11) · Pores(10) · Darkening(8) |

Click a keyword to display related comments in the right panel.

---

### 7. Audience & Inflow — Viewer Type Analysis

![Audience Inflow 1](./image/화면5.png)
![Audience Inflow 2](./image/화면6.png)

- **Viewer Persona**: Loyal 25.2% · Newbie 26.2% · None 48.6%
- **Comment Inflow Timing**: Early 74.9% (positive 94.1%) · Expansion 18.2% · Steady 6.8%

---

### 8. Total Comments

![Total Comments](./image/화면7.png)

Browse all comments filtered by positive / negative / crisis / A·B test. Each comment shows sentiment label, crisis level, and inflow timing.

---

## 📦 Tech Stack

![Python](https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-000000?style=flat-square&logo=flask&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?style=flat-square&logo=postgresql&logoColor=white)
![Azure](https://img.shields.io/badge/Azure-0078D4?style=flat-square&logo=microsoftazure&logoColor=white)
![Azure Functions](https://img.shields.io/badge/Azure%20Functions-0062AD?style=flat-square&logo=azurefunctions&logoColor=white)
![OpenAI](https://img.shields.io/badge/Azure%20OpenAI-412991?style=flat-square&logo=openai&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-2088FF?style=flat-square&logo=githubactions&logoColor=white)
