const { getEducationItems } = require('../../utils/education')

Page({
  data: {
    items: []
  },

  onLoad() {
    this.setData({ items: getEducationItems() })
  }
})
