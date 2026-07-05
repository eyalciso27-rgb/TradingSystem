תאריך: 5 ביולי 2026

סטטוס: Baseline v0.5; חיבורי משתמש ל-WhatsApp, Discord ו-Telegram הם יכולת חובה; Telegram Bot API הוא מסלול משלים; עבר בדיקת עקביות סופית ומוכן לתכנית יישום, אך טרם אושר למסחר אוטומטי.

| המערכת היא מערכת רב-מודלית שמסוגלת לגלות, לנתח ולבצע עסקאות גם כאשר WhatsApp, Discord או Telegram אינם זמינים. עם זאת, User Adapter לכל אחד משלושת המקורות הוא יכולת מימוש חובה. אין מודל יחיד או פלטפורמה חיצונית ששולטת בברוקר. |
|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 1. מטרות ועקרונות

המפרט מגדיר את ששת מנועי המחקר, חוזי הפלט שלהם, מנגנון Signal Fusion, מודל הכיול, שילובי האסטרטגיות והגבולות בין מחקר, החלטה, סיכון וביצוע.

> **•** Candidate Discovery עצמאי ממקורות חברתיים.
>
> **•** Technical/Fundamental/News/ML/External/Social הם Assessments נפרדים.
>
> **•** Risk ו-Execution נשארים דטרמיניסטיים ומופרדים.
>
> **•** כל נתון ו-Model Output הם Point-in-Time וגרסאיים.
>
> **•** אין Direct-to-Broker signal.

# 2. ארכיטקטורת מנועים

| **מנוע**         | **קלט**                                                   | **פלט**                                            |
|------------------|-----------------------------------------------------------|----------------------------------------------------|
| Technical        | Quotes, bars, volume, calendar, sector/index              | TechnicalAssessment + levels/trigger/invalidation. |
| Fundamental      | Statements, estimates, ratios, sector peers, InvestingPro | FundamentalAssessment + watchlist eligibility.     |
| News/NLP         | News, SEC, IR, market notices, public web evidence        | Catalyst/Sentiment/Materiality/Novelty assessment. |
| Quant/ML         | Versioned features and approved artifacts                 | Regime, rank or calibrated probability.            |
| External Signals | TradingView/other provider payloads                       | CanonicalExternalSignal.                           |
| Social Signals   | WhatsApp/Discord/Telegram/manual source plans             | SocialAssessment and source instruction.           |
| Signal Fusion    | All assessments + strategy manifest                       | FusionRun and Decision evidence.                   |

# 3. Candidate Discovery

| **Channel**      | **Mechanism**                       | **Dependency**         |
|------------------|-------------------------------------|------------------------|
| Technical scan   | Scheduled/incremental market scan   | Market Data only.      |
| Fundamental scan | Daily/weekly scoring and watchlists | Fundamental provider.  |
| Event scan       | News/filings/IR events              | News/official sources. |
| External alert   | Webhook/API                         | Provider optional.     |
| Social           | Message/event                       | Optional.              |
| Manual           | Operator creates candidate          | Dashboard.             |

| היעדר WhatsApp, Discord, Telegram או TradingView אינו מעביר את המערכת ל-DOWN. רק אסטרטגיות שדורשות במפורש מקור כזה מושפעות. |
|-----------------------------------------------------------------------------------------------------------------------------|

# 4. Technical Analysis Engine

> **•** Core features: OHLCV, returns, gaps, ATR/volatility, moving averages, RSI/MACD or equivalent momentum, relative volume, spread, relative strength, support/resistance, highs/lows and market/sector context.
>
> **•** Indicators are deterministic, versioned and covered by Golden Tests.
>
> **•** Technical signal includes entry trigger, invalidation, horizon, valid_until and evidence.
>
> **•** TradingView may alert, but internal calculation validates the trigger.

| **Initial pattern**            | **MVP**           |
|--------------------------------|-------------------|
| Breakout with relative volume  | Core              |
| Trend/moving-average alignment | Core filter       |
| Pullback in trend              | Shadow            |
| Volatility contraction         | Shadow            |
| Mean reversion                 | Deferred strategy |
| Tick/HFT microstructure        | Out of scope      |

# 5. Fundamental Analysis Engine

> **•** Point-in-time normalization of statements, estimates, ratios, currency and corporate actions.
>
> **•** Separate scores for quality, growth, valuation and financial risk.
>
> **•** Watchlist membership has valid_from, available_at and invalidation reasons.
>
> **•** Fair-value models are model/version specific; no single universal valuation.

| **Model family** | **Examples**                                                |
|------------------|-------------------------------------------------------------|
| Quality          | Margins, cash conversion, ROIC/ROE, balance sheet.          |
| Growth           | Revenue/EPS/FCF growth and estimate revisions.              |
| Valuation        | P/E, EV/EBITDA, P/S, DCF/relative valuation as applicable.  |
| Risk             | Leverage, dilution, liquidity, accounting/corporate events. |

# 6. News, NLP and Sentiment Engine

> **•** Classifies catalyst type, direct relevance, materiality, novelty, official-source status, correction/retraction and market reaction.
>
> **•** Sentiment without materiality is not a hard veto.
>
> **•** Copied headlines are clustered and counted once.
>
> **•** Official negative events can veto entry or trigger position review.
>
> **•** LLM output is structured and validated; raw article text never becomes an order command.

# 7. Quantitative and ML Engine

> **•** Initial tasks: market/sector regime classification, candidate ranking and anomaly detection.
>
> **•** Future task: calibrated probability of target-before-stop, subject to sufficient labels and leakage-safe evaluation.
>
> **•** Training is offline; inference loads only approved artifacts.
>
> **•** No online learning, no automatic hyperparameter changes, no ML-controlled risk sizing.

| **Lifecycle** | **Gate**                                                        |
|---------------|-----------------------------------------------------------------|
| Dataset       | Point-in-time, labels available after cutoff, hash and lineage. |
| Train         | Offline, reproducible environment.                              |
| Evaluate      | Out-of-sample, calibration, regime slices, costs.               |
| Replay        | Same events and policies.                                       |
| Shadow        | Champion/challenger on live events.                             |
| Promote       | Human approval and active pointer.                              |
| Rollback      | Immediate pointer reversal.                                     |

# 8. External Signals and TradingView

> **•** TradingView is one ExternalSignalProvider, not a system dependency.
>
> **•** Webhook payload is untrusted evidence and cannot contain executable broker authority.
>
> **•** Every alert is idempotent, time-limited, symbol-resolved and revalidated with internal data.
>
> **•** Pine Script logic may be mirrored or independently calculated for critical triggers.
>
> **•** Provider outage affects only dependent strategies.

# 9. Social Signals

> • WhatsApp, Discord and Telegram can create candidates, source plans and updates.
>
> **•** Social signals require independent confirmation for automated strategies unless a future manifest explicitly proves otherwise.
>
> **•** Source performance is segmented by author/channel/strategy/horizon.
>
> **•** No social source can bypass Max Chase, News Veto, Risk or Emergency Halt.

# 10. Canonical SignalAssessment

> {
>
> "assessment_id": "uuid7",
>
> "instrument_id": "instrument-master-id",
>
> "signal_family": "TECHNICAL\|FUNDAMENTAL\|NEWS\|QUANT_ML\|EXTERNAL\|SOCIAL",
>
> "model_id": "...",
>
> "model_version": "...",
>
> "direction": "LONG\|NEUTRAL\|NEGATIVE",
>
> "status": "TRIGGERED\|ELIGIBLE\|BLOCKED\|INCONCLUSIVE\|EXPIRED",
>
> "score": 82,
>
> "calibrated_probability": 0.67,
>
> "calibration_status": "CALIBRATED",
>
> "observed_at": "...",
>
> "available_at": "...",
>
> "valid_until": "...",
>
> "evidence": \[\],
>
> "risk_flags": \[\],
>
> "invalidation": {}
>
> }

# 11. Signal Fusion

> **•** A manifest declares REQUIRED, OPTIONAL, VETO and CONTEXT assessments.
>
> **•** Hard gates always precede weighted scoring or model probabilities.
>
> **•** No blind averaging of heterogeneous scores.
>
> **•** Fusion output records missing evidence, vetoes, trigger owner and exit policy.

| **Pattern**            | **Required**                                               | **Veto**                     |
|------------------------|------------------------------------------------------------|------------------------------|
| Techno-Fundamental     | Fundamental eligibility + technical trigger                | Material negative news/risk. |
| Technical + Sentiment  | Technical trigger                                          | Material negative catalyst.  |
| Fundamental + Catalyst | Quality/value + positive catalyst + technical confirmation | Overextension/event risk.    |
| Social Confirmed       | Social candidate + independent technical/news checks       | Source ambiguity/max chase.  |
| Technical + ML Regime  | Technical trigger + calibrated compatible regime           | Unverified/drifted model.    |
| Ensemble               | Manifest-specific                                          | Any declared hard veto.      |

# 12. Decision, Risk and Execution Boundary

> **•** Research engines produce assessments only.
>
> **•** Signal Fusion proposes an outcome; Decision Engine applies policy.
>
> **•** Risk Engine controls sizing and veto.
>
> **•** Final Pre-Submit refreshes market, approval, account, calendar and kill state.
>
> **•** Broker Adapter receives canonical OrderIntent only.

# 13. Testing and Evaluation

| **Test**               | **Requirement**                                        |
|------------------------|--------------------------------------------------------|
| Golden indicators      | Known bars produce exact expected indicators/triggers. |
| Point-in-time backtest | No record with available_at after decision.            |
| News clustering        | Copied articles do not create false consensus.         |
| Calibration            | Reliability/Brier or approved metrics by slice.        |
| Ablation               | Measure value of each assessment family.               |
| Costs                  | Fees, spread, slippage and fill assumptions.           |
| Regime robustness      | Results across trend/range/high-volatility periods.    |
| Failure degradation    | Required model down blocks only dependent strategy.    |

# 14. MVP and Roadmap

| **Capability**        | **MVP**                | **Later**                                    |
|-----------------------|------------------------|----------------------------------------------|
| Technical scanner     | Core                   | More patterns and optimization.              |
| Fundamental watchlist | Rules-based core       | Advanced valuation/forecasting.              |
| News/NLP              | Catalyst and veto core | Broader web/social NLP.                      |
| TradingView           | Adapter and validation | Other signal platforms.                      |
| ML                    | Regime/ranking Shadow  | Calibrated probability and portfolio models. |
| Statistical arbitrage | No                     | Long/short multi-leg roadmap.                |

# 15. Acceptance Criteria

| **ID**   | **Criterion**                                               |
|----------|-------------------------------------------------------------|
| MM-AC-01 | System discovers candidates with social adapters disabled.  |
| MM-AC-02 | Technical trigger is reproducible from stored bars.         |
| MM-AC-03 | Fundamental watchlist is point-in-time replayable.          |
| MM-AC-04 | News veto links to official/clustered evidence.             |
| MM-AC-05 | TradingView cannot submit orders directly.                  |
| MM-AC-06 | ML probability cannot gate Auto unless calibrated.          |
| MM-AC-07 | Fusion explains required, optional and veto evidence.       |
| MM-AC-08 | Every model output stores exact version and feature schema. |
| MM-AC-09 | Risk and kill switch override all analysis engines.         |
| MM-AC-10 | Statistical arbitrage remains out of MVP.                   |

# **16. גמישות Fusion ותלות חלקית במודלים**

| **Role**   | **התנהגות**                           |
|------------|---------------------------------------|
| REQUIRED   | חייב לעבור רק באסטרטגיה שהצהירה עליו. |
| VETO       | חוסם כאשר תנאי מהותי מתקיים.          |
| SOFT_SCORE | משפיע על ציון וגודל.                  |
| BONUS      | חיזוק בלבד; היעדרו אינו פוסל.         |
| CONTEXT    | מוצג ומעשיר הסבר.                     |

> **•** Missing assessment אינו שקול ל-Negative assessment.
>
> **•** Fusion מסיר כפילות בין מקורות שמתארים אותו אירוע.
>
> **•** Disagreement נשמר ומוצג; הוא יכול להעביר ל-AUTO_REDUCED, MANUAL או SHADOW.
>
> **•** Technical, Fundamental, News, ML, External ו-Social אינם חייבים לפעול כולם בכל אסטרטגיה.

# **17. Portfolio, Regime ו-Calibration**

> **•** Signal Fusion אינו מקצה הון; Portfolio Engine קובע Capacity לאחר Fusion.
>
> **•** Regime Engine מפיק Snapshot גרסאי ולא משנה מודלים בעצמו.
>
> **•** Opportunity Calibration מודד False Rejection באמצעות Counterfactual Shadow.
>
> **•** ML models מתחילים כ-Challengers ומקודמים רק לאחר Calibration ו-Out-of-Sample.

# **18. אבטחת תוכן**

> **•** כל טקסט אינטרנטי, Social או External Signal מסומן Untrusted Input.
>
> **•** מודלים מקבלים רק Data ולא Instructions לביצוע פעולות מערכת.
>
> **•** Webhook ו-Signal חייבים Provenance, timestamp ו-dedup key.

# **19. Telegram בתוך Social Signals Engine**

> **•** Telegram הוא Social Signal Source נוסף ולא מנוע החלטה נפרד.
>
> **•** Channel post, group message ו-forward מקבלים source_profile, author/sender metadata ו-trust assessment.
>
> **•** Forwarded content אינו מגדיל מספר אישורים אם הוא שייך לאותו Content Cluster.
>
> **•** BOT_API ו-USER_ACCOUNT_TDLIB נשמרים כ-access_mode לצורך מדידת אמינות ותקלות.
>
> **•** ל-Paper Auto, Signal חברתי מ-Telegram דורש Independent Confirmation אלא אם Manifest עתידי אושר אחרת.
>
> **•** היעדר Telegram לעולם אינו פוסל אסטרטגיה שאינה מגדירה אותו REQUIRED.

# **20. משמעות חיבורי המשתמש ל-Signal Fusion**

> **•** WhatsApp User, Discord User ו-Telegram User הם Source Observations נפרדים, אך אף אחד מהם אינו REQUIRED גלובלי להחלטה.
>
> **•** חובת המימוש של ה-Adapters אינה הופכת את זמינותם בזמן אמת לתנאי למסחר עצמאי.
>
> **•** Telegram Bot הוא observation channel נוסף; קליטה כפולה מאותו post אינה מעלה את ציון הקונצנזוס.
>
> **•** כל Social Signal ל-Paper Auto דורש Independent Confirmation לפי Manifest, ללא קשר למסלול User או Bot.
