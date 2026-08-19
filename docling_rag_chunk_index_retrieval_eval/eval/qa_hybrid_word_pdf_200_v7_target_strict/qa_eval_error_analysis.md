# QA 200 Full Eval v7 Target Strict Error Analysis

## Summary
- Total: 200
- Correct: 191
- Accuracy: 0.9550
- Evidence hit rate: 1.0000
- No lexical final: True
- Second pass count: 37

## Failure Counts
- correct: 191
- api_failed: 6
- llm_choice_error_after_recall: 3

## Wrong / Non-correct Items
### Q112
- Source type: word
- QA type: 多事实检索
- Pred / Gold: API_FAILED / B
- Failure type: api_failed
- Evidence hit: True
- LLM backend: api_failed_no_final
- API error: HTTPSConnectionPool(host='dashscope.aliyuncs.com', port=443): Max retries exceeded with url: /compatible-mode/v1/chat/completions (Caused by SSLError(SSLZeroReturnError(6, 'TLS/SSL connection has been closed (EOF) (_ssl.c:1147)')))
- Question: 关于《账簿划分和名词解释》，下列哪一组选项中的两项表述均属于该材料内容？
- Top evidence: chk_409_text_000001 | dense+sparse | 409_商业银行资本管理办法_附件13：账簿划分和名词解释.docx
- Analysis: retrieval/evidence succeeded, final answer failed because LLM API exhausted retries; this should be retried or served from cache in demo/eval.

### Q114
- Source type: word
- QA type: 单事实检索
- Pred / Gold: API_FAILED / D
- Failure type: api_failed
- Evidence hit: True
- LLM backend: api_failed_no_final
- API error: HTTPSConnectionPool(host='dashscope.aliyuncs.com', port=443): Max retries exceeded with url: /compatible-mode/v1/chat/completions (Caused by SSLError(SSLZeroReturnError(6, 'TLS/SSL connection has been closed (EOF) (_ssl.c:1147)')))
- Question: 检索《商业银行信息披露内容和要求》后，以下哪一项与材料内容一致？
- Top evidence: chk_417_tbl_417_0016_rows_0024_0036 | source_hint | 417_商业银行资本管理办法_附件22：商业银行信息披露内容和要求.docx
- Analysis: retrieval/evidence succeeded, final answer failed because LLM API exhausted retries; this should be retried or served from cache in demo/eval.

### Q128
- Source type: word
- QA type: 单事实检索
- Pred / Gold: API_FAILED / B
- Failure type: api_failed
- Evidence hit: True
- LLM backend: api_failed_no_final
- API error: HTTPSConnectionPool(host='dashscope.aliyuncs.com', port=443): Max retries exceeded with url: /compatible-mode/v1/chat/completions (Caused by SSLError(SSLZeroReturnError(6, 'TLS/SSL connection has been closed (EOF) (_ssl.c:1147)')))
- Question: 根据《账簿划分和名词解释》，下列哪项表述正确？
- Top evidence: chk_409_text_000001 | dense+sparse | 409_商业银行资本管理办法_附件13：账簿划分和名词解释.docx
- Analysis: retrieval/evidence succeeded, final answer failed because LLM API exhausted retries; this should be retried or served from cache in demo/eval.

### Q162
- Source type: word
- QA type: 单事实检索
- Pred / Gold: API_FAILED / D
- Failure type: api_failed
- Evidence hit: True
- LLM backend: api_failed_no_final
- API error: HTTPSConnectionPool(host='dashscope.aliyuncs.com', port=443): Max retries exceeded with url: /compatible-mode/v1/chat/completions (Caused by ProxyError('Unable to connect to proxy', RemoteDisconnected('Remote end closed connection without response')))
- Question: 检索《商业银行信息披露内容和要求》后，以下哪一项与材料内容一致？
- Top evidence: chk_417_tbl_417_0016_rows_0024_0036 | source_hint | 417_商业银行资本管理办法_附件22：商业银行信息披露内容和要求.docx
- Analysis: retrieval/evidence succeeded, final answer failed because LLM API exhausted retries; this should be retried or served from cache in demo/eval.

### Q168
- Source type: word
- QA type: 单事实检索
- Pred / Gold: C / B
- Failure type: llm_choice_error_after_recall
- Evidence hit: True
- LLM backend: dashscope_force_choice
- Question: 检索《账簿划分和名词解释》后，以下哪一项与材料内容一致？
- Top evidence: chk_409_text_000001 | dense+sparse | 409_商业银行资本管理办法_附件13：账簿划分和名词解释.docx
- Analysis: target evidence was recalled, but the choice model selected the wrong option; next optimization should focus on option-level evidence verification and passing stronger direct evidence snippets.

### Q176
- Source type: word
- QA type: 单事实检索
- Pred / Gold: C / B
- Failure type: llm_choice_error_after_recall
- Evidence hit: True
- LLM backend: dashscope_force_choice
- Question: 根据《账簿划分和名词解释》，下列哪项表述正确？
- Top evidence: chk_409_text_000001 | dense+sparse | 409_商业银行资本管理办法_附件13：账簿划分和名词解释.docx
- Analysis: target evidence was recalled, but the choice model selected the wrong option; next optimization should focus on option-level evidence verification and passing stronger direct evidence snippets.

### Q192
- Source type: word
- QA type: 单事实检索
- Pred / Gold: C / B
- Failure type: llm_choice_error_after_recall
- Evidence hit: True
- LLM backend: dashscope_force_choice
- Question: 检索《账簿划分和名词解释》后，以下哪一项与材料内容一致？
- Top evidence: chk_409_text_000001 | dense+sparse | 409_商业银行资本管理办法_附件13：账簿划分和名词解释.docx
- Analysis: target evidence was recalled, but the choice model selected the wrong option; next optimization should focus on option-level evidence verification and passing stronger direct evidence snippets.

### Q282
- Source type: pdf
- QA type: 单事实检索
- Pred / Gold: API_FAILED / D
- Failure type: api_failed
- Evidence hit: True
- LLM backend: api_failed_no_final
- API error: HTTPSConnectionPool(host='dashscope.aliyuncs.com', port=443): Max retries exceeded with url: /compatible-mode/v1/chat/completions (Caused by SSLError(SSLZeroReturnError(6, 'TLS/SSL connection has been closed (EOF) (_ssl.c:1147)')))
- Question: 根据《知识产权金融生态综合试点工作方案》，下列哪项表述正确？
- Top evidence: chk_386_text_000000 | dense+sparse+source_hint | 386_国家金融监督管理总局_国家知识产权局_国家版权局关于印发《知识产权金融生态综合试点工作方案》的通知_知识产权金融生态综合试点工作方案.pdf
- Analysis: retrieval/evidence succeeded, final answer failed because LLM API exhausted retries; this should be retried or served from cache in demo/eval.

### Q284
- Source type: pdf
- QA type: 多事实检索
- Pred / Gold: API_FAILED / B
- Failure type: api_failed
- Evidence hit: True
- LLM backend: api_failed_no_final
- API error: HTTPSConnectionPool(host='dashscope.aliyuncs.com', port=443): Max retries exceeded with url: /compatible-mode/v1/chat/completions (Caused by ProxyError('Unable to connect to proxy', RemoteDisconnected('Remote end closed connection without response')))
- Question: 关于《中资商业银行行政许可事项申请材料目录及格式要求（2023年版）》，下列哪一组选项中的两项表述均属于该材料内容？
- Top evidence: chk_430_text_000001 | dense+sparse | 430_中国银保监会关于印发中资商业银行行政许可事项申请材料_目录及格式要求的通知_附件：中资商业银行行政许可事项申请材料目录及格式要求（2023年.pdf
- Analysis: retrieval/evidence succeeded, final answer failed because LLM API exhausted retries; this should be retried or served from cache in demo/eval.
