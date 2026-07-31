function joinUrl(baseUrl, path) {
  if (/^https?:\/\//.test(path)) return path
  if (!baseUrl) return path
  return `${baseUrl.replace(/\/$/, '')}/${path.replace(/^\//, '')}`
}

function appendPart(parts, boundary, headers, value) {
  parts.push(`--${boundary}\r\n${headers}\r\n\r\n${value}\r\n`)
}

function buildMultipart({ walkingText, standingText, mapping, boundary }) {
  const selectedBoundary = boundary || `stepwise-${Date.now()}-${Math.random().toString(16).slice(2)}`
  const parts = []
  appendPart(
    parts,
    selectedBoundary,
    'Content-Disposition: form-data; name="walking"; filename="walking.txt"\r\nContent-Type: text/plain; charset=utf-8',
    walkingText
  )
  if (standingText) {
    appendPart(
      parts,
      selectedBoundary,
      'Content-Disposition: form-data; name="standing"; filename="standing.txt"\r\nContent-Type: text/plain; charset=utf-8',
      standingText
    )
  }
  appendPart(
    parts,
    selectedBoundary,
    'Content-Disposition: form-data; name="sensor_mapping"',
    JSON.stringify(mapping || {})
  )
  parts.push(`--${selectedBoundary}--\r\n`)
  return {
    body: parts.join(''),
    contentType: `multipart/form-data; boundary=${selectedBoundary}`
  }
}

function apiError(response, fallbackCode) {
  const payload = (response && response.data) || {}
  const detail = payload.error || {}
  const error = new Error(detail.message || `StepWise request failed (${response && response.statusCode})`)
  error.code = detail.code || fallbackCode || 'request_failed'
  return error
}

function artifactUrls(baseUrl, runId, artifacts) {
  const urls = {}
  ;(artifacts || []).forEach((artifact) => {
    const key = String(artifact.name || '').replace(/[^a-zA-Z0-9]+/g, '_').replace(/^_|_$/g, '')
    if (key) {
      urls[key] = joinUrl(
        baseUrl,
        `/api/v1/analyses/${runId}/artifacts/${encodeURIComponent(artifact.name)}`
      )
    }
  })
  return urls
}

function createAnalysisClient({ baseUrl = '', request, sleep }) {
  const wait = sleep || ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)))

  async function analyze({ walkingText, standingText = '', mapping = {}, pollIntervalMs = 500, maxPolls = 240 }) {
    const multipart = buildMultipart({ walkingText, standingText, mapping })
    const createdResponse = await request({
      url: joinUrl(baseUrl, '/api/v1/analyses'),
      method: 'POST',
      header: { 'content-type': multipart.contentType },
      data: multipart.body
    })
    if (createdResponse.statusCode !== 202) throw apiError(createdResponse, 'create_failed')
    const created = createdResponse.data
    let terminal = created
    for (let attempt = 0; attempt < maxPolls; attempt += 1) {
      if (terminal.status === 'succeeded' || terminal.status === 'failed') break
      await wait(pollIntervalMs)
      const statusResponse = await request({
        url: joinUrl(baseUrl, created.status_url),
        method: 'GET'
      })
      if (statusResponse.statusCode !== 200) throw apiError(statusResponse, 'status_failed')
      terminal = statusResponse.data
    }
    if (terminal.status === 'failed') throw apiError({ statusCode: 409, data: terminal }, 'analysis_failed')
    if (terminal.status !== 'succeeded') {
      const error = new Error('Analysis did not finish before polling expired.')
      error.code = 'poll_timeout'
      throw error
    }
    const resultResponse = await request({
      url: joinUrl(baseUrl, created.result_url),
      method: 'GET'
    })
    if (resultResponse.statusCode !== 200) throw apiError(resultResponse, 'result_failed')
    const result = resultResponse.data || {}
    const cards = result.risk_cards || []
    return Object.assign({}, result, {
      run_id: created.run_id,
      generated_at: created.created_at || '',
      cards,
      top_result: cards.length ? cards[0].title : 'No clear posture-risk card',
      top_level: cards.length ? cards[0].level : 'Low',
      urls: artifactUrls(baseUrl, created.run_id, result.artifacts)
    })
  }

  return { analyze }
}

module.exports = {
  buildMultipart,
  createAnalysisClient,
  joinUrl
}
