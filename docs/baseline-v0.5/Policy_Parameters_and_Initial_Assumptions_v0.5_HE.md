תאריך: 5 ביולי 2026

**סטטוס: גרסה מלאה ומתוקנת לאחר Consolidated Architecture Review; מועמדת ל-Baseline פנימי; אין למסור ל-Claude Code לפני השלמת Traceability Matrix ואישור מפורש.**

**המסמך שלפניך הוא מסמך מלא, לא מסמך Patch. כל תיקוני CAR שולבו בגוף ובנספח הקנוני. במקרה של סתירה שנותרה, ההגדרות הקנוניות וה-State Machines שבגרסה זו גוברות.**

| **הערכים במסמך אינם תחזית רווח, המלצת השקעה או הוכחה סטטיסטית. הם נקודת פתיחה שמרנית שנועדה לאפשר איסוף נתונים בטוח. כל ערך מקבל Version, רמת ביטחון, תנאי בחינה מחדש ויכולת Rollback. אין להטמיע אף אחד מהערכים כקבוע בקוד.** |
|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

# 0. מסמכי בסיס והיקף

| **מסמך**                                   | **תפקיד**                                               |
|--------------------------------------------|---------------------------------------------------------|
| Decision Contract v0.8                     | סמכויות, Hard Veto, כניסה ויציאה.                       |
| Strategy and Decision Framework v0.5       | אסטרטגיות, הערכה, Risk ו-Learning.                      |
| MVP Scope v0.6                             | מניות ו-ETF בארה״ב, Long בלבד ו-IBKR Paper.             |
| Security and Operations Specification v0.6 | Kill Switch, Broker Protection, Audit ו-Recovery.       |
| Technology and Deployment Decisions v0.6   | Config Loader, Policies, Services ו-Broker Abstraction. |

> **•** כל הערכים חלים על Shadow, Paper Manual ו-Paper Auto בלבד.
>
> **•** Paper Auto יוגבל תחילה לאסטרטגיה אחת ול-Universe מצומצם.
>
> **•** ערך מספרי שאינו מופיע במסמך נשאר Config Placeholder ואינו מומצא על ידי Claude Code.
>
> **•** המעבר ל-Live ידרוש Policy Set חדש ולא שימוש אוטומטי בערכי Paper.

# 1. ארכיטקטורת הפרמטרים

| **שכבה**           | **תפקיד**                                        | **אופן שינוי**                                      |
|--------------------|--------------------------------------------------|-----------------------------------------------------|
| Safety Envelope    | גבולות מוחלטים שאסור ל-Strategy לעבור.           | שינוי OWNER, Audit, Re-auth ו-Restart/Reload מבוקר. |
| Environment Policy | הבדלים בין Shadow, Paper Manual ו-Paper Auto.    | Active Version Pointer.                             |
| Strategy Policy    | TTL, Score, Entry, Stop, Target ו-Exit.          | גרסה לכל אסטרטגיה.                                  |
| Source Policy      | אמינות, Samples, Freshness ו-Allowlist.          | גרסה לכל מקור/ערוץ.                                 |
| Execution Policy   | Order types, timeouts, slippage ו-partial fills. | גרסה לכל Broker Capability Profile.                 |
| Promotion Policy   | תנאי מעבר בין מצבים.                             | אישור אנושי בלבד.                                   |

## 1.1 מבנה של כל Parameter

> {
>
> "parameter_id": "risk.per_trade_pct",
>
> "version": "0.1.0",
>
> "value": 0.0025,
>
> "unit": "fraction_of_risk_budget_equity",
>
> "environment": \["SHADOW", "PAPER_MANUAL", "PAPER_AUTO"\],
>
> "confidence": "LOW\|MEDIUM\|HIGH",
>
> "rationale": "conservative_initial_assumption",
>
> "valid_from": "2026-07-05T00:00:00Z",
>
> "review_after": {
>
> "closed_trades": 50,
>
> "calendar_days": 30
>
> },
>
> "allowed_range": {
>
> "min": 0.0010,
>
> "max": 0.0050
>
> },
>
> "owner": "RISK_POLICY",
>
> "status": "ACTIVE"
>
> }

## 1.2 כלל שינוי

> **•** שינוי Value יוצר Version חדש; אין עריכה של גרסה שכבר השתתפה בהחלטה.
>
> **•** שינוי בתוך Allowed Range יכול לעבור Replay ו-Shadow ללא שינוי קוד.
>
> **•** שינוי Safety Envelope דורש Change Request מחמיר יותר.
>
> **•** שינוי אלגוריתם נעשה באמצעות Plug-in/Strategy Version חדש, לא באמצעות הסתרת לוגיקה ב-YAML.
>
> **•** Rollback מחזיר את Active Pointer לגרסה קודמת.

# 2. הון ייחוס לניסוי

| **אין לחשב סיכון לפי יתרת Paper גדולה ולא מציאותית. לפני Paper Manual חובה להגדיר Simulation Equity Cap המשקף את סדר הגודל של ההון שאותו רוצים לדמות בעתיד.** |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------|

> risk_budget_equity =
>
> min(
>
> broker_net_liquidation_value,
>
> configured_simulation_equity_cap
>
> )

| **Parameter**              | **ערך ראשוני**                      | **יחידה** | **ביטחון**  |
|----------------------------|-------------------------------------|-----------|-------------|
| simulation_equity_cap      | חובה להזנה לפני Paper Manual        | USD       | אין Default |
| risk_budget_equity_refresh | לפני כל Decision ולפחות כל 60 שניות | זמן       | MEDIUM      |
| equity_high_watermark      | צילום יומי לצורך Drawdown           | USD       | MEDIUM      |

# 3. Safety Envelope ראשוני

| **Parameter**                 | **ערך** | **משמעות**               | **ביטחון** |
|-------------------------------|---------|--------------------------|------------|
| risk_per_trade_pct            | 0.25%   | מקסימום ראשוני 0.50%     | LOW        |
| max_total_open_risk_pct       | 1.00%   | סכום הסיכון עד כל Stops  | MEDIUM     |
| daily_soft_halt_pct           | 0.75%   | עצירת כניסות ובדיקה      | LOW        |
| daily_hard_halt_pct           | 1.00%   | Entry Halt אוטומטי       | MEDIUM     |
| weekly_hard_halt_pct          | 2.50%   | Entry Halt עד Review     | LOW        |
| monthly_drawdown_halt_pct     | 5.00%   | Demotion ל-Manual/Shadow | LOW        |
| max_concurrent_positions      | 4       | בכל החשבון               | LOW        |
| max_symbol_notional_pct       | 10.00%  | מהון הייחוס              | LOW        |
| max_single_order_notional_pct | 7.50%   | מהון הייחוס              | LOW        |
| max_sector_notional_pct       | 25.00%  | לפי Sector Mapping       | LOW        |
| max_correlated_cluster_pct    | 20.00%  | Cluster/Theme            | LOW        |
| max_source_open_risk_share    | 50.00%  | מתוך Open Risk           | LOW        |

## 3.1 כללים קשיחים

> **•** אין Averaging Down ב-MVP.
>
> **•** אין יותר מפוזיציה אחת פעילה לאותו Symbol, אלא אם Policy עתידית מגדירה אחרת.
>
> **•** אין Pyramiding או הגדלה אוטומטית של פוזיציה קיימת.
>
> **•** אין כניסה חדשה כאשר Daily/Weekly/Monthly Halt פעיל.
>
> **•** Risk Engine רשאי להקטין Quantity; אינו רשאי להגדיל מעבר לבקשת Strategy.
>
> **•** Quantity מעוגלת כלפי מטה כדי לא לחרוג מתקציב הסיכון.

# 4. Universe, נזילות וזכאות

| **Parameter**             | **ערך ראשוני** | **תחולה**                           |
|---------------------------|----------------|-------------------------------------|
| asset_class               | STOCK, ETF     | כל המצבים                           |
| direction                 | LONG_ONLY      | כל המצבים                           |
| min_price_auto            | \$5.00         | PAPER_AUTO                          |
| min_price_manual          | \$3.00         | PAPER_MANUAL; מתחת לכך Review מוגבר |
| min_adv20_dollar_auto     | \$20M          | PAPER_AUTO                          |
| min_adv20_dollar_manual   | \$10M          | PAPER_MANUAL                        |
| max_median_spread_auto    | 0.30%          | RTH                                 |
| max_median_spread_manual  | 0.50%          | RTH                                 |
| min_market_cap_auto       | \$500M         | או Exception מאושר                  |
| min_listing_age_days_auto | 20 ימי מסחר    | אין IPO חדש                         |
| leveraged_inverse_etf     | DISABLED       | MVP                                 |
| otc                       | DISABLED       | MVP                                 |
| halted_or_suspended       | HARD_VETO      | כל המצבים                           |

## 4.1 חלונות זמן לביצוע

| **מסלול**                | **חלון**                     | **הערה**                                  |
|--------------------------|------------------------------|-------------------------------------------|
| PAPER_AUTO new entries   | 09:35-15:45 ET               | לא כולל 5 דקות ראשונות ו-15 דקות אחרונות. |
| PAPER_MANUAL new entries | 09:30-15:55 ET               | Override מודע בלבד מחוץ לחלון Auto.       |
| Pre/After Market         | איסוף וניתוח בלבד            | אין ביצוע אוטומטי.                        |
| Near Close               | ניהול יציאות בלבד לפי Policy | אין כניסה חדשה Auto.                      |

# 5. איכות נתונים, ישויות והוראות

| **Parameter**                                         | **ערך ראשוני** | **הערה**                  |
|-------------------------------------------------------|----------------|---------------------------|
| entity_calibrated_confidence_auto                     | \>= 0.98       | טיקר ונכס                 |
| entity_heuristic_or_calibrated_confidence_manual      | \>= 0.85       | אחרת INVALID/REVIEW       |
| instruction_calibrated_confidence_auto                | \>= 0.95       | Entry/Target/Stop/Intent  |
| instruction_heuristic_or_calibrated_confidence_manual | \>= 0.80       | מתחת לכך REVIEW           |
| data_quality_auto                                     | \>= 0.90       | שלמות, Freshness ו-Health |
| data_quality_manual                                   | \>= 0.80       | Warning גלוי              |
| market_snapshot_age_auto                              | \<= 3 שניות    | בעת Decision/Order        |
| market_snapshot_age_manual                            | \<= 10 שניות   | בעת Approval              |
| entity_candidates_auto                                | 1 בלבד         | אין עמימות                |
| model_schema_validation                               | 100%           | אחרת INVALID              |

## 5.1 טענות מהותיות

> **•** טענה מהותית בלתי מאומתת ממקור חברתי אינה מספיקה לבדה ל-PAPER_AUTO.
>
> **•** מקור רשמי, דיווח חברה או News Cluster אמין יכולים לאמת Catalyst.
>
> **•** העתקות של אותה ידיעה אינן נחשבות מקורות בלתי תלויים.
>
> **•** כאשר אי אפשר לאמת טענה בזמן, התוצאה היא WATCH, SHADOW או PAPER_MANUAL.

# 6. Freshness, TTL ו-Deadline

| **Signal/Action**             | **ערך ראשוני**      | **הערה**                         |
|-------------------------------|---------------------|----------------------------------|
| SOCIAL_EXPLICIT_INTRADAY      | 15 דקות לכניסה Auto | אם אין Valid Until מפורש.        |
| SOCIAL_EXPLICIT_SWING         | עד סוף יום המסחר    | Auto אינו מותר ללא Horizon ברור. |
| social.callout_confirmation   | 10 דקות             | מגיל ההודעה.                     |
| news.catalyst_momentum_AUTO   | 3 דקות              | לאחר מכן WATCH/Manual.           |
| news.catalyst_momentum_MANUAL | 10 דקות             | בכפוף למחיר ול-R/R.              |
| MANUAL_CANDIDATE              | 60 דקות             | ברירת מחדל.                      |
| AUTO_DECISION_EXPIRY          | 30 שניות            | לאחר Market Snapshot הסופי.      |
| MANUAL_DECISION_EXPIRY        | 120 שניות           | לאחר Market Snapshot הסופי.      |
| FAST_APPROVAL_EXPIRY          | 60 שניות            | נדרש Refresh לאחר פקיעה.         |
| STANDARD_APPROVAL_EXPIRY      | 180 שניות           | נדרש Refresh לאחר פקיעה.         |

## 6.1 חריגה מ-Deadline

> **•** אין להשתמש בנתון ישן כאילו הוא עדכני.
>
> **•** AUTO עובר ל-WATCH או EXPIRE; אינו ממשיך לפקודה.
>
> **•** Manual מציג שההחלטה פגה ודורש Refresh מלא.
>
> **•** Latency ו-Deadline Miss נשמרים לצורך החלפת ההנחות בעתיד.

# 7. כניסה, Max Chase ויחס סיכוי-סיכון

| **Parameter/Instruction**    | **ערך**                                                    | **ביטחון/כלל**                          |
|------------------------------|------------------------------------------------------------|-----------------------------------------|
| BUY_LIMIT_MAX                | אין ביצוע מעל מחיר המקסימום המאושר.                        | קשיח                                    |
| BUY_ZONE                     | אין ביצוע מעל קצה האזור העליון.                            | קשיח                                    |
| BUY_STOP_TRIGGER             | Max Chase = הקטן מבין 0.50% ו-0.25 ATR14.                  | LOW                                     |
| ENTER_IMMEDIATELY            | Max Chase = הקטן מבין 0.75% ו-0.25 ATR14 מהמחיר בזמן האות. | LOW                                     |
| max_reward_consumed_pct_auto | 25%                                                        | מהמרחק בין Entry המקורי ל-Target.       |
| min_reward_risk_manual       | 1.50R                                                      | אחרי עלויות משוערות.                    |
| min_reward_risk_auto         | 1.75R                                                      | אחרי עלויות משוערות.                    |
| target_missing_auto          | REJECT                                                     | אלא אם Strategy Exit Policy מגדירה יעד. |
| stop_missing_auto            | REJECT                                                     | אין עסקה בלי Exit Plan.                 |

## 7.1 מודל עלויות ראשוני לצורך Decision

| **Parameter**            | **ערך ראשוני**                   | **הערה**                     |
|--------------------------|----------------------------------|------------------------------|
| estimated_entry_slippage | max(0.05%, half_spread)          | לצורך R/R בלבד.              |
| estimated_exit_slippage  | max(0.05%, half_spread)          | לצורך R/R בלבד.              |
| fees                     | Actual broker schedule כאשר זמין | אחרת Config.                 |
| paper_fill_adjustment    | נמדד בנפרד                       | לא מניחים ש-Paper משקף Live. |

# 8. Stop, יציאה והגנת פוזיציה

| **אין Stop אחיד של 4% לכל העסקאות. ה-Stop נקבע לפי הוראת מקור תקפה, מבנה אסטרטגי או מנגנון תנודתיות מאושר. האחוז משמש לחישוב הסיכון לאחר שנקודת הביטול הוגדרה.** |
|------------------------------------------------------------------------------------------------------------------------------------------------------------------|

| **עדיפות** | **מקור Stop**            | **פעולה**                          |
|------------|--------------------------|------------------------------------|
| 1          | Source Explicit Stop     | אם ברור, חוקי ועומד בבדיקות.       |
| 2          | Strategy Structural Stop | שפל/תמיכה/Invalidation מוגדרים.    |
| 3          | ATR/Volatility Stop      | רק באסטרטגיה שמגדירה זאת.          |
| 4          | No Valid Stop            | REJECT או MANUAL REVIEW; אין Auto. |

| **Parameter**                        | **ערך ראשוני**                     | **הערה**               |
|--------------------------------------|------------------------------------|------------------------|
| min_stop_distance                    | max(0.50%, 2 x spread, 0.25 ATR14) | Auto                   |
| max_stop_distance_auto               | 8.00%                              | מעבר לכך Manual/Reject |
| protective_order_type_auto           | STOP / broker-native stop-market   | לנכסים נזילים          |
| stop_limit_auto                      | DISABLED                           | סיכון לאי-ביצוע        |
| trailing_stop_default                | DISABLED                           | Strategy-specific בלבד |
| break_even_default                   | DISABLED                           | Strategy-specific בלבד |
| widen_stop_automatically             | PROHIBITED                         | אין הגדלת הפסד         |
| move_stop_toward_profit              | Policy/Manual only                 | נשמר כ-Revision        |
| position_without_required_protection | SEV-1 + Entry Halt                 | Paper Auto             |

## 8.1 סיכון ביצוע של Stops

> **•** Stop שמופעל הופך בדרך כלל לפקודת Market ולכן אינו מבטיח מחיר ביצוע.
>
> **•** Stop-Limit מגביל מחיר אך עלול לא להתבצע.
>
> **•** ב-MVP Auto נעדיף יציאה על פני הבטחת מחיר רק בנכסים שעומדים בספי הנזילות.
>
> **•** Slippage, Gaps ו-Halts נמדדים בנפרד ואינם נחשבים כשל תוכנה כשלעצמם.

# 9. ספי החלטה ראשוניים לפי אסטרטגיה

| Score הוא מדד תצוגה ו-Policy, לא הסתברות מכוילת. Hard Gates קודמים תמיד לציון. הספים הבאים הם הנחות ברמת ביטחון נמוכה ויוחלפו לאחר מדגם מספק. |
|-----------------------------------------------------------------------------------------------------------------------------------------------|

| **Strategy**                | **Paper Manual** | **Paper Auto**      | **הערה**                       |
|-----------------------------|------------------|---------------------|--------------------------------|
| social.explicit_plan        | 70               | 85                  | אסטרטגיה ראשונה המועמדת ל-Auto |
| social.callout_confirmation | 75               | לא ב-MVP Auto ראשון | דורשת אימות חזק יותר           |
| news.catalyst_momentum      | 80               | לא ב-MVP Auto ראשון | TTL קצר וסיכון Latency         |
| exit.position_management    | לא ציון גלובלי   | דטרמיניסטי/Policy   | Stops, targets ו-risk          |

## 9.1 טווחי Decision כלליים

| **Score**       | **Outcome**             | **הערה**                 |
|-----------------|-------------------------|--------------------------|
| \<55            | REJECT                  | כאשר אין Hard Veto אחר.  |
| 55-69           | WATCH/SHADOW            | לפי Strategy.            |
| 70+             | עשוי להיות Paper Manual | לפי סף האסטרטגיה.        |
| Auto Threshold+ | עשוי להיות Paper Auto   | רק לאחר כל Gates וקידום. |

## 9.2 הפעלת Critic

> **•** חובה כאשר קיימת טענה מהותית שאינה מאומתת.
>
> **•** חובה כאשר יש סתירה בין מקורות או Strategies.
>
> **•** חובה כאשר Source הוא UNRATED.
>
> **•** חובה כאשר Price Extension מעל 0.50 ATR מהנקודה הרלוונטית.
>
> **•** חובה כאשר Score נמצא עד 5 נקודות מסף האישור.
>
> **•** ב-FAST Path ניתן לדלג רק כאשר כל Hard Gates תקינים וה-Policy מאפשרת.

# **10. אמינות מקורות**

| **Parameter**               | **ערך ראשוני**    | **הערה**                     |
|-----------------------------|-------------------|------------------------------|
| new_source_status           | UNRATED           | אינו מקבל בונוס חיובי.       |
| provisional_sample_size     | 30 Signals סגורים | ציון זמני בלבד.              |
| auto_eligible_sample_size   | 75 Signals דומים  | אותה Strategy/Horizon.       |
| minimum_observation_days    | 60 ימים           | לפני Auto Eligibility.       |
| source_score_weight_cap     | 15%               | מה-Decision Score.           |
| recency_half_life           | 90 ימים           | LOW; לבחינה.                 |
| copied_message_independence | 0                 | אותו Cluster אינו מקור נוסף. |
| source_open_risk_cap        | 50% מ-Open Risk   | מונע ריכוז.                  |

## 10.1 מדדי מקור

> **•** Hit rate אינו מספיק לבדו.
>
> **•** נמדדים Expectancy, MFE, MAE, Time-to-Target, Stop Rate ו-Latency Sensitivity.
>
> **•** המדדים מופרדים לפי Strategy, Sector, Direction ו-Horizon.
>
> **•** מקור עם מעט Signals נשאר UNRATED או PROVISIONAL.
>
> **•** שינוי משקל מקור אינו אוטומטי ואינו מתבצע על בסיס החודש האחרון בלבד.

# 11. מדיניות פקודות וביצוע

| **Parameter**               | **ערך ראשוני**                           | **הערה**                     |
|-----------------------------|------------------------------------------|------------------------------|
| entry_order_auto            | LIMIT / STOP / STOP-LIMIT לפי Entry Type | אין Market Entry Auto        |
| entry_tif                   | DAY                                      | MVP                          |
| outside_rth                 | FALSE                                    | MVP Auto                     |
| fast_unfilled_timeout       | 60 שניות                                 | לאחר מכן Cancel remainder    |
| standard_unfilled_timeout   | 180 שניות                                | לאחר מכן Cancel remainder    |
| partial_fill_minimum        | כל Fill מנוהל                            | הגנה לפי הכמות שמולאה        |
| unknown_order_state         | RECONCILE, no blind retry                | קשיח                         |
| max_submit_attempts_unknown | 0                                        | לא שולחים שוב לפני Reconcile |
| broker_ack_timeout          | 10 שניות                                 | עובר ל-UNKNOWN/RECONCILE     |
| modify_cancel_timeout       | 10 שניות                                 | אחרת Reconcile               |

## 11.1 Partial Fill

> **•** כל כמות שמולאה יוצרת Position ומקבלת Protection.
>
> **•** היתרה מבוטלת לאחר Timeout או כאשר המחיר יצא מתכנית העסקה.
>
> **•** יעדים ו-Stop מותאמים לכמות שבוצעה בפועל.
>
> **•** אין שליחת Order נוסף כדי 'להשלים' כמות ללא Decision חדש.

# 12. Gates למעבר בין מצבים

## **12.1 מעבר משלב הצללים למסחר ידני בחשבון הדמו**

| **מדד**                             | **Gate ראשוני** |
|-------------------------------------|-----------------|
| משך רציף                            | לפחות 4 שבועות  |
| Candidates מעובדים                  | \>= 200         |
| Shadow Trades מלאים                 | \>= 50          |
| Ticker precision במדגם מתויג        | \>= 99%         |
| Entry/Target/Stop numeric precision | \>= 99%         |
| Intent/Entry type precision         | \>= 97%         |
| Update linking precision            | \>= 95%         |
| Replay consistency                  | \>= 99%         |
| Critical safety incidents           | 0               |

## **12.2 מעבר ממסחר ידני לאוטומציה מוגבלת בחשבון הדמו**

| **מדד**                              | **Gate ראשוני**                  |
|--------------------------------------|----------------------------------|
| Strategy                             | social.explicit_plan בלבד בתחילה |
| משך                                  | \>= 30 ימים רצופים               |
| Completed Paper Trades               | \>= 50                           |
| Duplicate Orders                     | 0                                |
| Environment Mismatch                 | 0                                |
| Unprotected Required Positions       | 0                                |
| Reconciliation after drills          | 100%                             |
| Critical safety incidents            | 0                                |
| Operational error rate               | \< 0.5%                          |
| Net expectancy after simulated costs | \> 0                             |
| Maximum Paper Drawdown               | \<= 5%                           |
| Source sample if source-dependent    | \>= 75 comparable signals        |

## 12.3 תנאי Demotion מיידי

> **•** כל Duplicate Order או Environment Mismatch.
>
> **•** פוזיציה הדורשת הגנה שנשארה ללא Stop אצל הברוקר.
>
> **•** Broker State שאינו ידוע ולא נפתר בזמן.
>
> **•** Daily/Weekly/Monthly Halt.
>
> **•** שגיאת Entity/Instruction שהובילה לפקודה שגויה.
>
> **•** כשל Critical ב-Kill Switch, Audit או Reconciliation.

## **12.4 תנאים לדיון עתידי במסחר חי**

| **מדד**                   | **דרישה**                             |
|---------------------------|---------------------------------------|
| Paper Duration            | לפחות 6 חודשים                        |
| Completed Trades          | \>= 200 באסטרטגיה המועמדת             |
| Critical Safety Incidents | 0 ב-90 הימים האחרונים                 |
| Operational Drills        | כולם עברו                             |
| Positive Expectancy       | אחרי Costs ובמדגם Out-of-Sample       |
| Drawdown                  | בתוך Envelope שאושר                   |
| Live Policy Set           | נפרד ומאושר                           |
| Decision                  | אישור אנושי מפורש; אין Auto Promotion |

# 13. קטלוג הנחות ראשוני

| **מזהה** | **הנחה**                                        | **ביטחון** | **כיצד מחליפים**                     |
|----------|-------------------------------------------------|------------|--------------------------------------|
| ASM-001  | 0.25% סיכון לעסקה מתאים להתחלה                  | LOW        | לאחר 50 ו-100 עסקאות                 |
| ASM-002  | Open Risk של 1% שמרני מספיק                     | LOW        | לאחר Drawdown ו-Correlation analysis |
| ASM-003  | TTL של 15 דקות להוראה חברתית מפורשת             | LOW        | Latency-decay study                  |
| ASM-004  | News Auto TTL של 3 דקות                         | LOW        | Event reaction analysis              |
| ASM-005  | Max Chase של 0.25 ATR/0.5%                      | LOW        | MFE/MAE by entry delay               |
| ASM-006  | R/R של 1.75R ל-Auto                             | LOW        | Expectancy by R/R bucket             |
| ASM-007  | Score 85 מתאים ל-Social Explicit Auto           | LOW        | Calibration after \>=200 decisions   |
| ASM-008  | Source sample 75 מספיק ל-Auto eligibility       | LOW        | Confidence interval analysis         |
| ASM-009  | Spread 0.30% ו-ADV \$20M מגנים מספיק מ-Slippage | LOW        | Execution-quality analysis           |
| ASM-010  | Stop-market עדיף ב-Auto liquid universe         | MEDIUM     | Paper/live-like slippage study       |
| ASM-011  | Critic בטווח 5 נקודות מהסף מוסיף ערך            | LOW        | Ablation test                        |
| ASM-012  | 4 פוזיציות במקביל מתאימות ל-MVP                 | LOW        | Capacity and correlation review      |

# 14. מנגנון החלפת הנחות בנתונים

| **שלב** | **שם**   | **פעולה**                                                     |
|---------|----------|---------------------------------------------------------------|
| 1       | Collect  | אוספים Events, Decisions, Fills, MFE/MAE, Errors ו-Latency.   |
| 2       | Label    | מסווגים איכות החלטה, תוצאה וסיבת כשל.                         |
| 3       | Segment  | מפרידים לפי Strategy, Source, Sector, Horizon ו-Regime.       |
| 4       | Propose  | יוצרים Change Proposal לפרמטר אחד או Change Set מוגבל.        |
| 5       | Replay   | בודקים Out-of-Sample וללא שימוש במידע עתידי.                  |
| 6       | Shadow   | Champion ו-Challenger מקבלים אותם Events.                     |
| 7       | Review   | בודקים Safety, Expectancy, Drawdown, Stability ו-Sample Size. |
| 8       | Promote  | Active Pointer מתחלף באישור אנושי.                            |
| 9       | Rollback | חזרה מיידית לגרסה קודמת אם Gate נשבר.                         |

## 14.1 כללי Anti-Overfitting

> **•** אין שינוי Parameter לאחר עסקה בודדת או רצף קצר.
>
> **•** לא משנים מספר פרמטרים יחד ללא Change Set מנומק.
>
> **•** שומרים Holdout/Out-of-Sample שאינו משמש להצעת השינוי.
>
> **•** בודקים תוצאות לפי Regimes ולא רק ממוצע כולל.
>
> **•** רווח מקרי אינו מכשיר החלטה גרועה.
>
> **•** כל Promotion דורש Sample Size מינימלי שנקבע מראש.

# 15. תדירות סקירה

| **סקירה**              | **תוכן**                                        | **פלט**                     |
|------------------------|-------------------------------------------------|-----------------------------|
| Daily Operations       | Health, errors, stops, reconciliation ו-halts.  | ללא שינוי Strategy אוטומטי  |
| Weekly Risk            | Drawdown, exposure, rejected orders ו-slippage. | שינוי Safety רק באירוע חריג |
| Monthly Policy         | Source, strategy, TTL, chase ו-decision bands.  | Change Proposals            |
| Milestone Review       | 50/100/200 Trades.                              | בחינה סטטיסטית              |
| Quarterly Architecture | Assumptions, data providers, models ו-brokers.  | Review רחב                  |

# 16. דוגמת קונפיגורציה

> policy_set:
>
> id: paper-conservative
>
> version: 0.1.0
>
> environment: PAPER
>
> equity:
>
> simulation_equity_cap: REQUIRED
>
> reference: MIN_BROKER_NLV_AND_CAP
>
> safety_envelope:
>
> risk_per_trade_pct: 0.0025
>
> max_total_open_risk_pct: 0.0100
>
> daily_soft_halt_pct: 0.0075
>
> daily_hard_halt_pct: 0.0100
>
> weekly_hard_halt_pct: 0.0250
>
> monthly_drawdown_halt_pct: 0.0500
>
> max_concurrent_positions: 4
>
> universe:
>
> auto:
>
> min_price: 5.00
>
> min_adv20_dollar: 20000000
>
> max_median_spread_pct: 0.0030
>
> min_market_cap: 500000000
>
> regular_hours_only: true
>
> data_quality:
>
> entity_calibrated_confidence_auto: 0.98
>
> instruction_calibrated_confidence_auto: 0.95
>
> min_data_quality_auto: 0.90
>
> max_snapshot_age_seconds_auto: 3
>
> execution:
>
> auto_market_entry: false
>
> outside_rth: false
>
> fast_unfilled_timeout_seconds: 60
>
> standard_unfilled_timeout_seconds: 180
>
> reconcile_before_retry_unknown: true
>
> stop_policy:
>
> trailing_default: false
>
> break_even_default: false
>
> auto_widen_stop: false
>
> max_stop_distance_pct_auto: 0.08
>
> promotion:
>
> automatic_promotion: false

# **17. מגבלות סביבת המסחר המדומה**

> **•** Paper fills הם סימולציה ואינם מוכיחים נזילות או Fill זהה ל-Live.
>
> **•** IBKR מציינת ש-Paper fills מבוססים על Top of Book ללא גישה לעומק מלא.
>
> **•** Stops ופקודות מורכבות מסוימות מדומות ב-Paper ועלולות להתנהג אחרת מחשבון Production.
>
> **•** לכן Performance Paper מחייב Haircut/Slippage Analysis ואינו עובר אוטומטית ל-Live.
>
> **•** כל Live Policy עתידית תיבנה מחדש מתוך נתוני Paper, Drills ו-Proof מוגבל.

# 18. קריטריוני קבלה

| **מזהה**  | **קריטריון**                                     |
|-----------|--------------------------------------------------|
| POL-AC-01 | כל Parameter נטען מקונפיגורציה גרסאית ולא מקוד.  |
| POL-AC-02 | Simulation Equity Cap חובה לפני Paper Manual.    |
| POL-AC-03 | Risk Engine משתמש בקטן מבין NLV ל-Cap.           |
| POL-AC-04 | שינוי Policy יוצר Version ו-Audit.               |
| POL-AC-05 | Rollback אינו דורש Build.                        |
| POL-AC-06 | Hard Gates קודמים ל-Score.                       |
| POL-AC-07 | Stop באחוזים אינו תקף ללא Anchor.                |
| POL-AC-08 | אין Paper Auto ללא Broker-side protection.       |
| POL-AC-09 | Unknown Broker State אינו גורם ל-Retry עיוור.    |
| POL-AC-10 | Source חדש נשאר UNRATED עד Sample מינימלי.       |
| POL-AC-11 | Paper Auto מוגבל תחילה לאסטרטגיה אחת.            |
| POL-AC-12 | אין Auto Promotion בין מצבים.                    |
| POL-AC-13 | כל Assumption כולל Review Trigger.               |
| POL-AC-14 | Decision Snapshot שומר את כל Parameter Versions. |
| POL-AC-15 | Live דורש Policy Set נפרד.                       |

# 19. החלטות פתוחות

| **מזהה**    | **החלטה**                                       |
|-------------|-------------------------------------------------|
| POL-OPEN-01 | Simulation Equity Cap בפועל.                    |
| POL-OPEN-02 | סכום עמלה ומודל Slippage לפי חשבון/Market Data. |
| POL-OPEN-03 | שדות InvestingPro שישפיעו בפועל על Score.       |
| POL-OPEN-04 | הגדרת Correlated Clusters.                      |
| POL-OPEN-05 | Time Stop לכל Strategy.                         |
| POL-OPEN-06 | מודל Target כאשר המקור לא מספק יעד.             |
| POL-OPEN-07 | מקור אמת ל-ATR, ADV, Spread ו-Market Cap.       |
| POL-OPEN-08 | מספר ה-Signals המדויק הנדרש לפי מקור.           |
| POL-OPEN-09 | Haircut שייושם על תוצאות Paper.                 |
| POL-OPEN-10 | ערכי Live עתידיים; אינם נקבעים כעת.             |

# 20. מקורות מקצועיים שנבדקו

> **•** U.S. Securities and Exchange Commission — Investor Bulletin: Stop, Stop-Limit, and Trailing Stop Orders.
>
> **•** FINRA — Stop Orders: Factors to Consider During Volatile Markets.
>
> **•** Interactive Brokers — Paper Trading vs Live Trading: Differences and Simulator Limitations.
>
> **•** Interactive Brokers — TWS API Documentation and Bracket/OCA Order Documentation.

# 21. השלב הבא

> **•** Consolidated Architecture Review — בדיקת כל המסמכים כמכלול אחד.
>
> **•** Traceability Matrix — קישור כל דרישה לרכיב, Schema, Test ו-Criterion.
>
> **•** Gap and Contradiction Register — רשימת סתירות, החלטות פתוחות וחוסרים.
>
> **•** Final Specification Pack — גרסאות מאוחדות ונקיות.
>
> **•** Claude Code Implementation Plan — רק לאחר אישור חבילת האפיון הסופית.

# 22. Policy Baseline מתוקן

| **הערכים המספריים נשארים הנחות שמרניות. הפרק מתקן את הגדרת Confidence, נוסחאות P&L, Pre-Submit, Calendar, Corporate Actions ו-Promotion Evidence.** |
|-----------------------------------------------------------------------------------------------------------------------------------------------------|

## 22.1 Confidence Gates

| **Parameter**                          | **ערך/כלל**               |
|----------------------------------------|---------------------------|
| calibration_status_auto                | CALIBRATED בלבד.          |
| model_reported_confidence              | Telemetry; אינו Gate.     |
| entity_calibrated_confidence_auto      | \>= 0.98 לאחר Benchmark.  |
| instruction_calibrated_confidence_auto | \>= 0.95 לאחר Benchmark.  |
| heuristic_confidence                   | Shadow/Manual בלבד.       |
| benchmark_version                      | חובה ב-Decision Snapshot. |

## 22.2 P&L ו-Drawdown

> adjusted_equity = broker_nlv - net_external_cash_flows
>
> session_pnl = adjusted_equity - session_start_adjusted_equity
>
> daily_loss_pct = min(0, session_pnl / session_start_risk_budget_equity)
>
> weekly_loss_pct = min(0, adjusted_equity - week_start_adjusted_equity) / week_start_risk_budget_equity
>
> drawdown_pct = (adjusted_equity - equity_high_watermark) / equity_high_watermark
>
> **•** Realized ו-Unrealized נכללים דרך NLV.
>
> **•** Cash flows מנוטרלים.
>
> **•** Snapshot חסר או Broker unsynced חוסמים כניסות.
>
> **•** High Watermark נשמר יומית ועם כל שינוי הון חיצוני.

## 22.3 Final Pre-Submit Parameters

| **בדיקה**                | **כלל**                                     |
|--------------------------|---------------------------------------------|
| market_snapshot_age      | \<= 3s Auto; \<= 10s Manual.                |
| approval_age             | בתוך expiry.                                |
| calendar_state           | OPEN וכניסה לפני cutoff.                    |
| kill_state               | כל הרמות OFF.                               |
| broker_state             | HEALTHY + RECONCILED.                       |
| risk_recheck             | APPROVE או REDUCE.                          |
| whole_share_rounding     | floor; quantity \>= 1.                      |
| submit_authorization_ttl | 10 שניות ראשוני; LOW confidence assumption. |

## 22.4 Entry Types

> **•** BUY_LIMIT_MAX — אין מעבר מעל המחיר.
>
> **•** BUY_STOP_TRIGGER — Max Chase לפי Policy.
>
> **•** BUY_ZONE — ביצוע בתוך הטווח בלבד.
>
> **•** ENTER_IMMEDIATELY — Marketable Limit או פקודה שאושרה; לא Market Entry Auto.
>
> **•** BUY_PULLBACK ו-CONDITIONAL_ENTRY — רק Strategy שמגדירה תנאי מלא.

## 22.5 Calendar and Corporate Actions

> **•** כל חלון זמן מחושב לפי Calendar Version ו-America/New_York.
>
> **•** Early Close מזיז Cutoffs באופן יחסי/גרסאי.
>
> **•** Corporate Action conflict מפעיל SYMBOL_HALT.
>
> **•** אין Auto Entry/Exit discretionary עד Reconciliation; protective broker orders מטופלים לפי Runbook.

## 22.6 News Policy

> **•** Official filings מותרים לפי Field/Timestamp validation.
>
> **•** External news provider disabled by default עד PoI.
>
> **•** news.catalyst_momentum Paper Auto disabled ב-v0.2.
>
> **•** Copied stories have independence weight 0 בתוך אותו cluster.

## 22.7 Operational לעומת Statistical Gates

| **Gate**        | **מטרה**                                                                                       |
|-----------------|------------------------------------------------------------------------------------------------|
| Operational     | דיוק parsing, zero duplicates, zero env mismatch, protection, reconciliation, uptime/drills.   |
| Statistical     | expectancy after costs, confidence interval, holdout, regime coverage, stability and drawdown. |
| Paper Auto      | Operational pass + minimum evidence; אינו הוכחת Live alpha.                                    |
| Live discussion | שני ה-Gates + separate Live policy + explicit approval.                                        |

## 22.8 Source Score As-of

> **•** Decision משתמש ב-source_score_snapshot שהיה valid בזמן האות.
>
> **•** Sample updates אינם משנים היסטוריה.
>
> **•** 75 signals הוא eligibility floor בלבד, לא הוכחה סטטיסטית.
>
> **•** Weight change עובר Replay/Shadow ואישור.

# 23. פרמטרים ראשוניים למחקר רב-מודלי ו-Fusion

| **הפרמטרים הבאים הם Hypotheses ל-Replay/Shadow. הם אינם הסתברויות אמת ואינם מוטמעים בקוד. Paper Auto דורש Gates, Calibration ו-Data Quality לכל Assessment המשמש כתנאי.** |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------|

## 23.1 Technical Scanner

| **Parameter**            | **ערך התחלתי**      | **סטטוס**                     |
|--------------------------|---------------------|-------------------------------|
| breakout_min_score       | 75/100              | HEURISTIC; Shadow calibration |
| relative_volume_min      | 1.5                 | Strategy-specific             |
| max_spread_pct           | לפי Safety Envelope | Hard gate                     |
| max_trigger_age          | 120 שניות           | Replay/Shadow                 |
| technical_data_freshness | \<= 3 שניות ל-Auto  | Hard gate                     |

## 23.2 Fundamental Eligibility

| **Parameter**          | **ערך התחלתי**      | **הערה**                        |
|------------------------|---------------------|---------------------------------|
| quality_score_min      | 70                  | Rules-based, לא probability.    |
| growth_score_min       | 60                  | Strategy-specific.              |
| valuation_score_min    | 50                  | Peer/sector normalized.         |
| financial_risk_allowed | LOW\|MEDIUM         | HIGH is veto/manual.            |
| snapshot_max_age       | לפי field ownership | Statements vs estimates differ. |

## 23.3 News/NLP Gate

| **מצב**                             | **פעולה**                          |
|-------------------------------------|------------------------------------|
| Official material negative catalyst | VETO/EXIT REVIEW.                  |
| Unverified negative headline        | WATCH/critic; not automatic veto.  |
| Duplicate/copied cluster            | one evidence cluster only.         |
| No recent news                      | UNKNOWN/NO_EVIDENCE; not positive. |
| News assessment stale               | not eligible as Auto gate.         |

## 23.4 Quant/ML

> **•** calibration_status must be CALIBRATED before probability can gate Paper Auto.
>
> **•** Initial ML tasks are regime classification and candidate ranking in REPLAY/SHADOW.
>
> **•** No ML-controlled position sizing, stop widening or automatic parameter adaptation.
>
> **•** Drift threshold breach moves model to HALTED or SHADOW according to policy.

## 23.5 Fusion Rules

> **•** Required assessment missing =\> WATCH/INCONCLUSIVE.
>
> **•** Veto assessment active =\> REJECT unless explicit manual exception policy exists.
>
> **•** Optional assessments can add explanatory score only within a capped contribution.
>
> **•** No arithmetic average across incomparable scores.
>
> **•** Every Fusion version declares trigger_owner and exit_policy.

## 23.6 Initial Strategy Gates

| **Strategy**                    | **Required**                                          | **Veto**                                     | **Auto status**                   |
|---------------------------------|-------------------------------------------------------|----------------------------------------------|-----------------------------------|
| technical.breakout_news_guard   | technical score\>=75, liquidity eligible              | material negative news, risk-off, stale data | First candidate after validation. |
| techno_fundamental.breakout     | fundamental eligibility + technical trigger           | financial high risk, negative catalyst       | Shadow/Manual initially.          |
| social.independent_confirmation | social candidate + independent technical confirmation | max chase/news/risk                          | Optional Manual.                  |
| technical.regime_filtered       | technical trigger + calibrated compatible regime      | unverified model                             | Shadow only.                      |

## 23.7 External Signal TTL

| **Signal**                       | **TTL ראשוני**                        | **כלל**                                     |
|----------------------------------|---------------------------------------|---------------------------------------------|
| TradingView intraday alert       | 5 דקות                                | Revalidate price, indicator and session.    |
| Technical internal trigger       | 2 דקות                                | Strategy can shorten.                       |
| Fundamental watchlist membership | עד next scheduled refresh             | Corporate/news event may invalidate sooner. |
| News catalyst                    | לפי event class; initial 3-10 minutes | Must track novelty and market reaction.     |

# **21. מדיניות גמישות, קיפאון ופעילות יתר**

| **Parameter**                       | **ערך פתיחה** | **הערה**                                   |
|-------------------------------------|---------------|--------------------------------------------|
| auto_full_risk_multiplier           | 1.00          | כפוף ל-Safety Envelope.                    |
| auto_reduced_risk_multiplier        | 0.70          | אי-ודאות שאינה בטיחותית.                   |
| manual_default_risk_multiplier      | 0.40          | ניתן לאישור עד תקרת Policy.                |
| starvation_window_sessions          | 10            | LOW confidence; לבחינה.                    |
| starvation_min_shadow_eligible      | 30            | Alert רק כאשר היו הזדמנויות סבירות.        |
| gate_concentration_alert_pct        | 50%           | Gate לא-בטיחותי יחיד לאורך 5 Sessions.     |
| minimum_counterfactual_tracking_pct | 100%          | ל-Candidates בעלי Entry/Stop/Target תקפים. |
| overtrading_ratio_alert             | 3x            | מול חציון 20 Sessions או Strategy budget.  |
| policy_change_auto_apply            | FALSE         | הצעה בלבד.                                 |

## **21.1 כללי פרשנות**

> **•** Starvation אינו מוגדר כאפס עסקאות בלבד; נדרש גם נפח Candidates שעברו Data/Safety eligibility.
>
> **•** אין הורדת סף אוטומטית ואין forced trade.
>
> **•** Gate בטיחות אינו נכנס לניתוח ריכוז לצורך הקלה.
>
> **•** Policy What-if משתמש ב-Replay/Counterfactual ואינו משנה Active Version.

# **22. פרמטרי Portfolio ו-Regime ראשוניים**

| **Parameter**                     | **ערך פתיחה**                                            |
|-----------------------------------|----------------------------------------------------------|
| strategy_risk_budget_share        | גרסאי; סכום כל ה-Budgets \<= 100% מה-Open Risk Envelope  |
| same_symbol_multiple_strategies   | DISABLED; Strategy Winner אחד                            |
| correlation_cluster_open_risk_cap | 20% מה-Open Risk                                         |
| unknown_regime_auto_behavior      | REDUCED או MANUAL לפי Strategy                           |
| major_event_window                | No new Auto entry כאשר Strategy Policy מגדירה event veto |
| regime_model_status               | HEURISTIC ACTIVE; ML SHADOW בתחילה                       |

# **23. Trust Modifiers**

> **•** UNVERIFIED/QUARANTINED content אינו משמש Required Evidence.
>
> **•** מקורות שהם אותו News Cluster אינם נחשבים אישורים עצמאיים.
>
> **•** Official negative catalyst יכול להיות VETO בהתאם ל-Strategy.
>
> **•** Social manipulation flags מעבירים Candidate ל-Manual/Shadow או REJECT לפי חומרה.

# **22. פרמטרים לחיבורי משתמש חברתיים**

| **Parameter**                                    | **Default**     | **Hardness**             |
|--------------------------------------------------|-----------------|--------------------------|
| social.whatsapp_user.capability_required         | true            | HARD IMPLEMENTATION GATE |
| social.discord_user.capability_required          | true            | HARD IMPLEMENTATION GATE |
| social.telegram_user.capability_required         | true            | HARD IMPLEMENTATION GATE |
| social.telegram_bot.enabled                      | false until PoI | CONFIGURABLE             |
| social.telegram_bot.can_satisfy_user_requirement | false           | HARD                     |
| social.runtime_required_globally                 | false           | HARD ARCHITECTURE RULE   |
| social.cross_mode_dedup_required                 | true            | HARD DATA RULE           |
