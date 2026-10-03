Output docs/demo/demo.gif
Output docs/demo/demo.mp4

Set Shell zsh
Set FontSize 15
Set Width 1280
Set Height 720
Set Padding 20
Set TypingSpeed 25ms
Set Theme "Builtin Dark"

Hide
Type "cd demo && export PS1='$ ' && clear"
Enter
Sleep 500ms
Show

Type "# Zoho Inventory connector (read-only MCP server), used by an agent"
Enter
Type "uv --directory .. run zoho-connector auth status | head -4"
Enter
Wait@30s /^\$$/
Sleep 2s

Type "python3 ask.py 'Customer paid with pay_TjJrIOvtskFJU8 two days ago but has not received anything. What is going on?'"
Enter
Wait@120s /^\$$/
Sleep 4s

Type "python3 ask.py 'Customer raised a dispute on pay_TjJqSqc2PW0zCs saying it was never delivered. What evidence do we have?'"
Enter
Wait@120s /^\$$/
Sleep 4s

Type "python3 ask.py 'Cancel order SO-00002 and refund the customer.'"
Enter
Wait@120s /^\$$/
Sleep 4s

Type "uv --directory .. run pytest -q | tail -1"
Enter
Wait@60s /^\$$/
Sleep 3s
