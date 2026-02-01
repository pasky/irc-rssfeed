#!/bin/sh

while true; do
	echo -e "\n\n\n-------- Started at "`date`
	"$@" 2>&1
	sleep 60
done
