Output docs/demo/demo.gif
Output docs/demo/demo.mp4

Set Shell zsh
Set FontSize 15
Set Width 1280
Set Height 720
Set Padding 20
Set TypingSpeed 20ms
Set Theme "Builtin Dark"

Hide
Type "cd demo && setopt interactivecomments && export PS1='$ ' && clear"
Enter
Sleep 500ms
Show

Type "# Zoho Inventory connector (read-only MCP server), used by an agent"
Enter
Sleep 1s
Type `python3 ask.py "Customer paid with pay_TjJrIOvtskFJU8 two days ago but got nothing. What happened?"`
Enter
Wait@120s /^\$$/
Sleep 4s

Hide
Type "clear"
Enter
Sleep 300ms
Show
Type `python3 ask.py "Why hasn't pay_TjJr1DLusM3dSE shipped?"`
Enter
Wait@120s /^\$$/
Sleep 4s

Hide
Type "clear"
Enter
Sleep 300ms
Show
Type `python3 ask.py "Find the order for pay_TjJqkl40gFyOIK"`
Enter
Wait@120s /^\$$/
Sleep 4s

Hide
Type "clear"
Enter
Sleep 300ms
Show
Type `python3 ask.py "Customer disputes pay_TjJqSqc2PW0zCs saying it was never delivered. What evidence do we have?"`
Enter
Wait@120s /^\$$/
Sleep 4s

Hide
Type "clear"
Enter
Sleep 300ms
Show
Type `python3 ask.py "Which paid orders have been waiting more than 3 days to ship?"`
Enter
Wait@120s /^\$$/
Sleep 4s

Hide
Type "clear"
Enter
Sleep 300ms
Show
Type `python3 ask.py "Cancel order SO-00002 and refund the customer"`
Enter
Wait@120s /^\$$/
Sleep 4s

Hide
Type "clear"
Enter
Sleep 300ms
Show
Type "uv --directory .. run pytest -q | tail -1"
Enter
Wait@60s /^\$$/
Sleep 3s
