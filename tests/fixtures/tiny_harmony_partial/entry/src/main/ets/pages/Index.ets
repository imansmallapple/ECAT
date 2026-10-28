// Partially migrated — still contains some Android patterns
@Entry
@Component
struct Index {
  private label: string = 'Initial'

  build() {
    Column() {
      Text(this.label)
        .fontSize(20)
      // TODO: replace with ArkTS click handler
      Button('Click me')
        .onClick(() => {
          // was: button.setOnClickListener(v -> label.setText("Clicked!"))
          this.label = 'Clicked!'
        })
    }
    .width('100%')
    .height('100%')
  }
}
