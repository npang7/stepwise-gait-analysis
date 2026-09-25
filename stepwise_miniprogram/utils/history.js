const HISTORY_KEY = 'stepwise_history'
const HISTORY_LIMIT = 20

function readHistory() {
  try {
    return wx.getStorageSync(HISTORY_KEY) || []
  } catch (err) {
    return []
  }
}

function writeHistory(items) {
  wx.setStorageSync(HISTORY_KEY, items.slice(0, HISTORY_LIMIT))
}

function formatTime(date) {
  const pad = (n) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function saveHistory(result) {
  const item = {
    id: result.run_id || String(Date.now()),
    createdAt: formatTime(new Date()),
    topResult: result.top_result || 'No clear posture-risk card',
    topLevel: result.top_level || 'Low',
    summary: result.summary || {},
    metrics: result.metrics || {},
    cards: result.risk_cards || result.cards || [],
    urls: result.urls || {},
    result
  }
  const history = readHistory().filter((oldItem) => oldItem.id !== item.id)
  writeHistory([item].concat(history))
  return item
}

function clearHistory() {
  wx.removeStorageSync(HISTORY_KEY)
}

module.exports = {
  readHistory,
  saveHistory,
  clearHistory
}
