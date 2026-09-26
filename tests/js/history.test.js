const assert = require('node:assert/strict')
const test = require('node:test')


test('local history saves normalized result cards and can be cleared', () => {
  const storage = new Map()
  global.wx = {
    getStorageSync: (key) => storage.get(key),
    setStorageSync: (key, value) => storage.set(key, value),
    removeStorageSync: (key) => storage.delete(key)
  }
  const history = require('../../stepwise_miniprogram/utils/history')
  const saved = history.saveHistory({
    run_id: 'run-1',
    top_result: 'Insufficient data',
    top_level: 'High',
    risk_cards: [{ title: 'Insufficient data', level: 'High' }],
    summary: { samples: 20 },
    metrics: {}
  })
  assert.equal(saved.id, 'run-1')
  assert.equal(saved.cards[0].title, 'Insufficient data')
  assert.equal(history.readHistory().length, 1)
  history.clearHistory()
  assert.deepEqual(history.readHistory(), [])
  delete global.wx
})
