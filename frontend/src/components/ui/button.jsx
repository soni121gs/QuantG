import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva } from "class-variance-authority";

import { cn } from "@/lib/utils"

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-[var(--qd-radius-sm)] border text-sm font-semibold transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--qd-focus)] disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "border-[var(--qd-accent)] bg-[var(--qd-accent)] text-[var(--qd-accent-contrast)] hover:bg-[var(--qd-accent-hover)]",
        primary:
          "border-[var(--qd-accent)] bg-[var(--qd-accent)] text-[var(--qd-accent-contrast)] hover:bg-[var(--qd-accent-hover)]",
        destructive:
          "border-[var(--qd-loss)] bg-[var(--qd-loss)] text-white hover:bg-rose-600",
        danger:
          "border-[var(--qd-loss)] bg-[var(--qd-loss)] text-white hover:bg-rose-600",
        success:
          "border-[var(--qd-profit)] bg-[var(--qd-profit)] text-[#04130a] hover:bg-emerald-500",
        warning:
          "border-[var(--qd-warn)] bg-[var(--qd-warn)] text-black hover:bg-amber-400",
        outline:
          "border-[var(--qd-border)] bg-transparent text-[var(--qd-text-2)] hover:border-[var(--qd-border-strong)] hover:bg-[var(--qd-surface-2)] hover:text-white",
        secondary:
          "border-[var(--qd-border)] bg-[var(--qd-surface-2)] text-white hover:bg-[var(--qd-surface-3)]",
        ghost: "border-transparent bg-transparent text-[var(--qd-text-2)] hover:bg-[var(--qd-surface-2)] hover:text-white",
        link: "text-primary underline-offset-4 hover:underline",
        icon:
          "border-[var(--qd-border)] bg-[var(--qd-surface)] text-[var(--qd-text-2)] hover:border-[var(--qd-border-strong)] hover:text-white",
      },
      size: {
        default: "h-9 px-4 py-2",
        md: "h-9 px-4 py-2",
        sm: "h-8 px-3 text-xs",
        lg: "h-11 px-5 text-sm",
        icon: "h-9 w-9 p-0",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

const Button = React.forwardRef(({ className, variant, size, asChild = false, ...props }, ref) => {
  const Comp = asChild ? Slot : "button"
  return (
    <Comp
      className={cn(buttonVariants({ variant, size, className }))}
      ref={ref}
      {...props} />
  );
})
Button.displayName = "Button"

export { Button, buttonVariants }
