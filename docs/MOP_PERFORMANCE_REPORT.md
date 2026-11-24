# MOP Reconciliation - performance report (24 Nov 2026)

Measured on the real production database and run (5,584,483 bill lines, 8 months, April to 14 November) with read-only queries, and on a development PC copy for the browser. Server and database names are not written here.

## 1. What a person waits for

| Action | Production database | Notes |
|---|---|---|
| Summary figures, whole range (8 months) | **3.5 s** | linear in the number of months loaded |
| Summary figures, one month | **0.47 s** | |
| Summary figures, one day | **0.04 s** | |
| Particulars matrix, whole range | 0.15 - 0.19 s | |
| Store cash card | 0.04 s | |
| Cash position, data (225 stores x 228 days) | about 1 s | the wide table then takes about 5 s to draw in the browser |
| 4 people loading the same month at once | 1.7 s each (0.55 s alone) | one process shares one interpreter; roughly linear beyond two users. Fine for a handful of staff; more would need a second instance |
| Float page | about 3.6 s warm | was far slower (82 s measured on the old path); see D-007 |

On the development PC copy (quiet machine) the whole range took 4.5 s and a month 0.6 s.

## 2. What a run costs

* A full run of 01/04 to 14/11 took about 36 minutes on the server (first production run), 40 minutes in earlier measurement, of which matching was about 6 minutes and saving results about 20 minutes. A run is always the whole range.
* A cold read of the large POS workbook is about 9 minutes; later runs read the cached copy.
* The development PC's PostgreSQL run took 40 minutes for the full range, and a run's time is dominated by saving results, so it grows with the number of months.

## 3. Findings that changed performance

* **D-006:** two page sections re-read every CMS file and rewrote the warehouse on each view. It froze a browser for minutes in testing and was seen live in the server log. Now loaded on request and cached until a file changes.
* **D-007:** Float used the slow raw-file path; now uses the precomputed figures (identical results).
* **Open bills page** and the bill search were already fixed earlier by index hints (23 s down to 0.9 s).

## 4. Capacity

| | Now | Comment |
|---|---|---|
| Application server | 27 GB memory (23 GB free), 376 GB free on the data drive; the warehouse is 1.2 GB | Plenty |
| Bill-lines table | 3.1 GB for 5.58 million rows, about 0.65 million rows a month | Plan retention before it passes about 15 GB |
| Database settings | Near the defaults (128 MB shared buffers, 4 MB work memory) on a shared database server where 52 of 100 connections are used by other applications | A DBA review would help; tables are vacuumed and the visibility map is complete |
| Index health | Every documented index exists; no duplicate index of identical columns; three small aggregate tables carry a prefix-redundant index (low priority) | |

## 5. Not measured

Load beyond 4 simultaneous users; network time from a user's PC to the server; the effect of the other applications sharing the database server at their busy times.
