const assert = require('node:assert/strict')
const test = require('node:test')

const {
  buildMultipart,
  createAnalysisClient
} = require('../../stepwise_miniprogram/utils/apiClient')


test('buildMultipart uploads walking, optional standing, and mapping fields', () => {
  const multipart = buildMultipart({
    walkingText: 'walking-data',
    standingText: 'standing-data',
    mapping: {
      heel: 'P2',
      arch: 'P3',
      medial_forefoot: 'P4',
      lateral_forefoot: 'P1',
      pitch_eversion_sign: 'positive'
    },
    boundary: 'stepwise-test'
  })
  assert.match(multipart.body, /name="walking"; filename="walking.txt"/)
  assert.match(multipart.body, /walking-data/)
  assert.match(multipart.body, /name="standing"; filename="standing.txt"/)
  assert.match(multipart.body, /standing-data/)
  assert.match(multipart.body, /name="sensor_mapping"/)
  assert.match(multipart.contentType, /boundary=stepwise-test/)
})


test('client uploads, polls, fetches, and normalizes a successful result', async () => {
  const calls = []
  const responses = [
    {
      statusCode: 202,
      data: {
        run_id: 'run-1',
        status: 'queued',
        status_url: '/api/v1/analyses/run-1',
        result_url: '/api/v1/analyses/run-1/result',
        created_at: '2026-08-01T00:00:00Z'
      }
    },
    { statusCode: 200, data: { run_id: 'run-1', status: 'running' } },
    { statusCode: 200, data: { run_id: 'run-1', status: 'succeeded' } },
    {
      statusCode: 200,
      data: {
        summary: { samples: 20 },
        metrics: { ArchRatio_mean: 0.1 },
        risk_cards: [{ title: 'Low-quality trial', level: 'High' }],
        artifacts: [{ name: 'user_report.html' }]
      }
    }
  ]
  const client = createAnalysisClient({
    baseUrl: 'https://stepwise.test',
    request: async (options) => {
      calls.push(options)
      return responses.shift()
    },
    sleep: async () => {}
  })

  const result = await client.analyze({ walkingText: 'walking-data', mapping: {} })

  assert.equal(calls[0].method, 'POST')
  assert.equal(calls[1].url, 'https://stepwise.test/api/v1/analyses/run-1')
  assert.equal(calls[3].url, 'https://stepwise.test/api/v1/analyses/run-1/result')
  assert.equal(result.run_id, 'run-1')
  assert.equal(result.top_result, 'Low-quality trial')
  assert.equal(result.top_level, 'High')
  assert.equal(result.cards.length, 1)
  assert.equal(
    result.urls.user_report_html,
    'https://stepwise.test/api/v1/analyses/run-1/artifacts/user_report.html'
  )
})


test('client stops polling and exposes the backend failure code', async () => {
  const responses = [
    {
      statusCode: 202,
      data: {
        run_id: 'run-2',
        status_url: '/api/v1/analyses/run-2',
        result_url: '/api/v1/analyses/run-2/result'
      }
    },
    {
      statusCode: 200,
      data: {
        status: 'failed',
        error: { code: 'analysis_timeout', message: 'Analysis timed out.' }
      }
    }
  ]
  const client = createAnalysisClient({
    request: async () => responses.shift(),
    sleep: async () => {}
  })
  await assert.rejects(
    client.analyze({ walkingText: 'walking-data', mapping: {} }),
    (error) => error.code === 'analysis_timeout'
  )
})
