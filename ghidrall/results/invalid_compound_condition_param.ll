; ModuleID = "invalid_compound_condition_param"
target triple = "x86_64-pc-linux-gnu"
target datalayout = "e-m:e-i64:64-f80:128-n8:16:32:64-S128"

define void @"sym.func_int"(i64 %".1") 
{
entry:
  %"var_4h" = alloca i64
  br label %"0x00000993"
"0x00000993":
  ret void
}

define void @"main"() 
{
entry:
  %"uVar1" = alloca i32
  br label %"0x000009cb"
"0x000009cb":
  %".3" = call i32 @nd()
  store i32 %".3", i32* %"uVar1"
  %".5" = load i32, i32* %"uVar1"
  %".6" = zext i32 %".5" to i64
  call void @"sym.func_int"(i64 %".6")
  ret void
}

declare i32 @nd() 