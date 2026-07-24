import { ButtonHTMLAttributes } from "react";
import { clsx } from "clsx";

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
};

export function Button({ className, variant = "secondary", ...props }: ButtonProps) {
  return <button className={clsx("button", `button-${variant}`, className)} {...props} />;
}
